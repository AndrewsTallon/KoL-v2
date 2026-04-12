from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Iterable, Optional

try:
    from dalicontrol.cct_utils import dtr_to_kelvin, level_to_pct
    from dalicontrol.paths import PROFILES_DIR
except ModuleNotFoundError:  # Allows direct execution from this folder.
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[2]))
    from dalicontrol.cct_utils import dtr_to_kelvin, level_to_pct
    from dalicontrol.paths import PROFILES_DIR


def _is_true(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _float_or_none(value) -> Optional[float]:
    try:
        if value in ("", None, "None"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_dt(row: dict, next_row: Optional[dict], fallback_s: float = 5.0) -> float:
    sample_dt = _float_or_none(row.get("sample_dt_s"))
    if sample_dt is not None and sample_dt >= 0:
        return sample_dt

    ts = _float_or_none(row.get("ts_epoch"))
    next_ts = _float_or_none((next_row or {}).get("ts_epoch"))
    if ts is not None and next_ts is not None and next_ts >= ts:
        return next_ts - ts

    return fallback_s


def _effective_brightness_pct(row: dict) -> float:
    if _is_true(row.get("lamp_is_off")):
        return 0.0

    pct = _float_or_none(row.get("lamp_brightness_pct"))
    if pct is not None:
        return pct

    level = _float_or_none(row.get("lamp_level"))
    if level is None:
        return 0.0
    return level_to_pct(int(level))


def _cct_kelvin(row: dict) -> Optional[int]:
    cct = _float_or_none(row.get("cct_kelvin"))
    if cct:
        return int(round(cct))

    dtr = _float_or_none(row.get("lamp_temp_dtr"))
    dtr1 = _float_or_none(row.get("lamp_temp_dtr1"))
    if dtr is None or dtr1 is None:
        return None
    cct = dtr_to_kelvin(int(dtr), int(dtr1))
    return cct or None


def _bucket(value: float, bucket_size: int) -> str:
    start = int(value // bucket_size) * bucket_size
    end = start + bucket_size
    return f"{start}-{end}"


def _load_final_evaluations(profiles_dir: Path = PROFILES_DIR) -> dict[str, list[dict]]:
    by_run: dict[str, list[dict]] = {}
    if not profiles_dir.exists():
        return by_run
    for path in profiles_dir.glob("*/final_evaluations/final_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        run = data.get("telemetry_run")
        if run:
            by_run.setdefault(Path(run).name, []).append(data)
    return by_run


def _questionnaire_summary(evaluations: Iterable[dict]) -> dict[str, float]:
    scores: dict[str, list[float]] = {f"q{i}": [] for i in range(1, 11)}
    for evaluation in evaluations:
        answers = evaluation.get("answers") or {}
        for key in scores:
            value = _float_or_none(answers.get(key))
            if value is not None:
                scores[key].append(value)
    return {key: round(mean(values), 2) for key, values in scores.items() if values}


def summarize_run(csv_path: Path, nominal_power_w: float = 40.0) -> dict:
    with csv_path.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    lit_runtime_s = 0.0
    absence_lit_s = 0.0
    energy_ws = 0.0
    brightness_on: list[float] = []
    brightness_dist = Counter()
    cct_dist = Counter()
    lux_values: list[float] = []
    action_counts = Counter()
    ai_reason_counts = Counter()

    for index, row in enumerate(rows):
        next_row = rows[index + 1] if index + 1 < len(rows) else None
        dt_s = _row_dt(row, next_row)
        brightness_pct = _effective_brightness_pct(row)
        is_on = not _is_true(row.get("lamp_is_off")) and brightness_pct > 0

        lux = _float_or_none(row.get("lux_smooth")) or _float_or_none(row.get("lux"))
        if lux is not None:
            lux_values.append(lux)

        cct = _cct_kelvin(row)
        if cct is not None:
            cct_dist[_bucket(cct, 500)] += 1

        action = str(row.get("action") or "").strip()
        if action:
            action_counts[action] += 1
            reason = str(row.get("reason") or "").strip()
            if reason:
                ai_reason_counts[reason] += 1

        if not is_on:
            continue

        lit_runtime_s += dt_s
        brightness_on.append(brightness_pct)
        brightness_dist[_bucket(brightness_pct, 10)] += 1
        energy_ws += nominal_power_w * (brightness_pct / 100.0) * dt_s
        if not _is_true(row.get("filt_occupied")):
            absence_lit_s += dt_s

    first = rows[0] if rows else {}
    last = rows[-1] if rows else {}
    return {
        "run": csv_path.name,
        "mode": first.get("mode", ""),
        "rows": len(rows),
        "start": first.get("ts_iso", ""),
        "end": last.get("ts_iso", ""),
        "lit_runtime_s": round(lit_runtime_s, 1),
        "absence_lit_s": round(absence_lit_s, 1),
        "absence_lit_pct": round(100 * absence_lit_s / lit_runtime_s, 1) if lit_runtime_s else 0.0,
        "estimated_energy_wh": round(energy_ws / 3600.0, 3),
        "avg_brightness_on_pct": round(mean(brightness_on), 1) if brightness_on else 0.0,
        "lux_avg": round(mean(lux_values), 1) if lux_values else 0.0,
        "lux_min": round(min(lux_values), 1) if lux_values else 0.0,
        "lux_max": round(max(lux_values), 1) if lux_values else 0.0,
        "action_count": sum(action_counts.values()),
        "top_actions": action_counts.most_common(5),
        "ai_reason_counts": ai_reason_counts.most_common(8),
        "brightness_distribution": brightness_dist.most_common(),
        "cct_distribution": cct_dist.most_common(),
    }


def render_markdown(summaries: list[dict], questionnaire_by_run: dict[str, list[dict]]) -> str:
    lines = [
        "# Thesis Telemetry Metrics",
        "",
        "| Run | Mode | Rows | Lit runtime (min) | Absence lit (%) | Energy (Wh) | Avg brightness on (%) | Lux avg | Actions |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in summaries:
        lines.append(
            f"| {item['run']} | {item['mode']} | {item['rows']} | "
            f"{item['lit_runtime_s'] / 60:.1f} | {item['absence_lit_pct']:.1f} | "
            f"{item['estimated_energy_wh']:.3f} | {item['avg_brightness_on_pct']:.1f} | "
            f"{item['lux_avg']:.1f} | {item['action_count']} |"
        )

    for item in summaries:
        lines.extend(["", f"## {item['run']}", ""])
        lines.append(f"- Time range: {item['start']} to {item['end']}")
        lines.append(f"- Lux range: {item['lux_min']} to {item['lux_max']} lux")
        lines.append(f"- Lighting during absence: {item['absence_lit_s'] / 60:.1f} minutes")
        lines.append(f"- Top AI/action reasons: {_format_counts(item['ai_reason_counts'])}")
        lines.append(f"- Brightness distribution: {_format_counts(item['brightness_distribution'])}")
        lines.append(f"- CCT distribution: {_format_counts(item['cct_distribution'])}")

        q_summary = _questionnaire_summary(questionnaire_by_run.get(item["run"], []))
        if q_summary:
            lines.append(f"- Linked questionnaire averages: {_format_counts(sorted(q_summary.items()))}")
        else:
            lines.append("- Linked questionnaire averages: none found")

    return "\n".join(lines) + "\n"


def _format_counts(items) -> str:
    if not items:
        return "none"
    return ", ".join(f"{key}: {value}" for key, value in items)


def write_csv_summary(path: Path, summaries: list[dict]) -> None:
    fieldnames = [
        "run",
        "mode",
        "rows",
        "start",
        "end",
        "lit_runtime_s",
        "absence_lit_s",
        "absence_lit_pct",
        "estimated_energy_wh",
        "avg_brightness_on_pct",
        "lux_avg",
        "lux_min",
        "lux_max",
        "action_count",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in summaries:
            writer.writerow({key: item.get(key, "") for key in fieldnames})


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize telemetry for thesis metrics.")
    parser.add_argument("runs", nargs="+", type=Path, help="Telemetry CSV files to summarize.")
    parser.add_argument("--nominal-power", type=float, default=40.0)
    parser.add_argument("--out-md", type=Path, default=None)
    parser.add_argument("--out-csv", type=Path, default=None)
    args = parser.parse_args()

    summaries = [summarize_run(path, args.nominal_power) for path in args.runs]
    questionnaire_by_run = _load_final_evaluations()
    markdown = render_markdown(summaries, questionnaire_by_run)

    if args.out_md:
        args.out_md.write_text(markdown, encoding="utf-8")
    else:
        print(markdown)

    if args.out_csv:
        write_csv_summary(args.out_csv, summaries)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
