"""
Participant profile storage for internal testing.

Profiles are not authentication identities. They are lightweight participant
selectors that keep questionnaire answers and learned AI models separated.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import PROFILES_DIR, PROFILES_PATH

logger = logging.getLogger(__name__)

AGE_GROUPS = ("18-29", "30-39", "40-49", "50-59", "60+")
LIKERT_MIN = 1
LIKERT_MAX = 5

_lock = threading.Lock()


def _utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _safe_id(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "participant"


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return default


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _registry_default() -> Dict[str, Any]:
    return {"active_profile_id": None, "profiles": []}


def _load_registry() -> Dict[str, Any]:
    data = _read_json(PROFILES_PATH, _registry_default())
    if not isinstance(data, dict):
        return _registry_default()
    profiles = data.get("profiles", [])
    if not isinstance(profiles, list):
        profiles = []
    return {
        "active_profile_id": data.get("active_profile_id"),
        "profiles": profiles,
    }


def _save_registry(data: Dict[str, Any]) -> None:
    _atomic_write_json(PROFILES_PATH, data)


def _profile_dir(profile_id: str) -> Path:
    return PROFILES_DIR / profile_id


def profile_model_dir(profile_id: str) -> Path:
    return _profile_dir(profile_id) / "models"


def participant_info_path(profile_id: str) -> Path:
    return _profile_dir(profile_id) / "participant_info.json"


def final_evaluations_dir(profile_id: str) -> Path:
    return _profile_dir(profile_id) / "final_evaluations"


def _next_profile_id(display_name: str, existing_ids: set) -> str:
    base = _safe_id(display_name)
    if base not in existing_ids:
        return base
    i = 2
    while f"{base}-{i}" in existing_ids:
        i += 1
    return f"{base}-{i}"


def _coerce_likert(value: Any, field: str) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer from 1 to 5")
    if not (LIKERT_MIN <= score <= LIKERT_MAX):
        raise ValueError(f"{field} must be from 1 to 5")
    return score


def validate_participant_info(payload: Dict[str, Any]) -> Dict[str, Any]:
    age_group = str(payload.get("age_group", "")).strip()
    if age_group not in AGE_GROUPS:
        raise ValueError("age_group must be one of: " + ", ".join(AGE_GROUPS))

    glasses = str(payload.get("glasses_or_contacts", "")).strip().lower()
    if glasses not in ("yes", "no"):
        raise ValueError("glasses_or_contacts must be yes or no")

    return {
        "age_group": age_group,
        "glasses_or_contacts": glasses,
        "brighter_lighting_preference": _coerce_likert(
            payload.get("brighter_lighting_preference"),
            "brighter_lighting_preference",
        ),
    }


def validate_final_evaluation(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = {}
    for i in range(1, 11):
        key = f"q{i}"
        result[key] = _coerce_likert(payload.get(key), key)
    result["comments"] = str(payload.get("comments", "")).strip()
    return result


def brightness_preference_from_score(score: int) -> float:
    score = _coerce_likert(score, "brighter_lighting_preference")
    return float(30 + 15 * (score - 1))


@dataclass
class ProfilePreferenceAdapter:
    brightness_pct: float
    completed: bool = True
    supports_cct_preference: bool = False

    def get_preferred_brightness(self, hour: float) -> float:
        return self.brightness_pct

    def get_preferred_cct(self, hour: float) -> int:
        return 0


def preference_adapter_for(profile_id: Optional[str]) -> Optional[ProfilePreferenceAdapter]:
    if not profile_id:
        return None
    info = get_participant_info(profile_id)
    if not info:
        return None
    return ProfilePreferenceAdapter(
        brightness_preference_from_score(info["brighter_lighting_preference"])
    )


def list_profiles() -> Dict[str, Any]:
    with _lock:
        registry = _load_registry()
        return {
            "profiles": list(registry["profiles"]),
            "active_profile_id": registry.get("active_profile_id"),
        }


def get_profile(profile_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not profile_id:
        return None
    registry = _load_registry()
    for profile in registry["profiles"]:
        if profile.get("profile_id") == profile_id:
            return dict(profile)
    return None


def get_active_profile() -> Optional[Dict[str, Any]]:
    registry = _load_registry()
    return get_profile(registry.get("active_profile_id"))


def create_profile(display_name: str, participant_info: Dict[str, Any]) -> Dict[str, Any]:
    name = str(display_name or "").strip()
    if not name:
        raise ValueError("display_name is required")
    clean_info = validate_participant_info(participant_info)

    with _lock:
        registry = _load_registry()
        existing_ids = {p.get("profile_id") for p in registry["profiles"]}
        profile_id = _next_profile_id(name, existing_ids)
        now = _utc_now()
        profile = {
            "profile_id": profile_id,
            "display_name": name,
            "created_at": now,
            "updated_at": now,
            "completed": True,
        }

        profile_dir = _profile_dir(profile_id)
        profile_model_dir(profile_id).mkdir(parents=True, exist_ok=True)
        final_evaluations_dir(profile_id).mkdir(parents=True, exist_ok=True)

        info_payload = dict(clean_info)
        info_payload.update({
            "profile_id": profile_id,
            "profile_name": name,
            "created_at": now,
            "updated_at": now,
        })
        _atomic_write_json(profile_dir / "participant_info.json", info_payload)

        registry["profiles"].append(profile)
        registry["active_profile_id"] = profile_id
        _save_registry(registry)

        return profile


def select_profile(profile_id: str) -> Dict[str, Any]:
    with _lock:
        registry = _load_registry()
        for profile in registry["profiles"]:
            if profile.get("profile_id") == profile_id:
                registry["active_profile_id"] = profile_id
                profile["updated_at"] = _utc_now()
                _save_registry(registry)
                return dict(profile)
    raise ValueError("Profile not found")


def get_participant_info(profile_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not profile_id:
        return None
    data = _read_json(participant_info_path(profile_id), None)
    return data if isinstance(data, dict) else None


def save_participant_info(profile_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    profile = get_profile(profile_id)
    if not profile:
        raise ValueError("Profile not found")
    clean_info = validate_participant_info(payload)
    now = _utc_now()
    clean_info.update({
        "profile_id": profile_id,
        "profile_name": profile["display_name"],
        "updated_at": now,
    })
    existing = get_participant_info(profile_id) or {}
    clean_info["created_at"] = existing.get("created_at", now)
    _atomic_write_json(participant_info_path(profile_id), clean_info)
    return clean_info


def list_final_evaluations(profile_id: str) -> List[Dict[str, Any]]:
    out = []
    directory = final_evaluations_dir(profile_id)
    if not directory.exists():
        return out
    for path in sorted(directory.glob("final_*.json"), reverse=True):
        data = _read_json(path, None)
        if isinstance(data, dict):
            summary = {
                "filename": path.name,
                "submitted_at": data.get("submitted_at"),
                "mode": data.get("mode"),
                "telemetry_run": data.get("telemetry_run"),
            }
            out.append(summary)
    return out


def save_final_evaluation(
    profile_id: str,
    payload: Dict[str, Any],
    *,
    mode: str = "",
    telemetry_run: str = "",
) -> Dict[str, Any]:
    profile = get_profile(profile_id)
    if not profile:
        raise ValueError("Profile not found")
    answers = validate_final_evaluation(payload)
    now = datetime.utcnow()
    submitted_at = now.replace(microsecond=0).isoformat() + "Z"
    result = {
        "profile_id": profile_id,
        "profile_name": profile["display_name"],
        "submitted_at": submitted_at,
        "mode": mode,
        "telemetry_run": telemetry_run,
        "answers": answers,
    }
    directory = final_evaluations_dir(profile_id)
    directory.mkdir(parents=True, exist_ok=True)
    filename = "final_" + now.strftime("%Y%m%d_%H%M%S_%f") + ".json"
    path = directory / filename
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    os.rename(tmp, path)
    result["filename"] = filename
    return result
