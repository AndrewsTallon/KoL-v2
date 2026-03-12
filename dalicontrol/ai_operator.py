import json
import importlib.util
import logging
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .dali_controls import DaliControls, clamp
from .dali_transport import DaliHidTransport
from .lamp_state import COOL_PRESET, LampController, LampState, WARM_PRESET
from .paths import STATE_PATH
MAX_ACTIONS_PER_SEC = 4


class NullControls:
    """No-op controls for dry runs; mirrors the LampController expectations."""

    def off(self):
        logging.info("[dry-run] off()")

    def set_arc_level(self, level: int):
        logging.info("[dry-run] set_arc_level(%s)", level)

    def dt8_set_temp_raw(self, dtr0: int, dtr1: int):
        logging.info("[dry-run] dt8_set_temp_raw(%s, %s)", dtr0, dtr1)

    def dt8_set_kelvin(self, kelvin: int):
        logging.info("[dry-run] dt8_set_kelvin(%s)", kelvin)


def load_state(path: Path = STATE_PATH) -> LampState:
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
            return LampState(
                last_level=int(payload.get("last_level", 254)),
                last_temp=tuple(payload.get("last_temp", COOL_PRESET)),
                is_off=bool(payload.get("is_off", False)),
            )
    except FileNotFoundError:
        return LampState()
    except Exception as exc:
        logging.warning("Failed to load state file %s: %s", path, exc)
        return LampState()


def save_state(state: LampState, path: Path = STATE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(asdict(state), f, indent=2)
    except Exception as exc:
        logging.error("Failed to persist state to %s: %s", path, exc)


class AIOperator:
    """
    AI operator handles ONLY:
    - user text commands
    - optional sensor status reporting

    Occupancy automation is handled in main.py
    """

    def __init__(self, lamp: LampController, state_path: Path = STATE_PATH, dry_run: bool = False):
        self.lamp = lamp
        self.state_path = state_path
        self.dry_run = dry_run

        self._action_times: List[float] = []
        self._openai_client = None
        self._openai_available_checked = False

    # ---------- LLM helpers ----------
    def _get_openai_client(self):
        if self._openai_available_checked:
            return self._openai_client

        self._openai_available_checked = True
        api_key = os.getenv("OPENAI_API_KEY")

        if not api_key:
            logging.info("OPENAI_API_KEY not set; using rules-based parser.")
            return None

        if importlib.util.find_spec("openai") is None:
            logging.info("OpenAI package not installed; using rules-based parser.")
            return None

        from openai import OpenAI

        try:
            self._openai_client = OpenAI(api_key=api_key)
            logging.info("OpenAI client initialized.")
        except Exception as exc:
            logging.warning("OpenAI unavailable, falling back to rules: %s", exc)
            self._openai_client = None

        return self._openai_client

    def _llm_plan(self, text: str) -> List[Dict[str, Any]]:
        client = self._get_openai_client()
        if client is None:
            return self._rules_plan(text)

        functions = [
            {
                "name": "set_actions",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": [
                                            "set_brightness_pct",
                                            "set_white",
                                            "set_yellow",
                                            "off",
                                            "on_last",
                                        ],
                                    },
                                    "pct": {"type": "number"},
                                },
                                "required": ["action"],
                            },
                        }
                    },
                    "required": ["actions"],
                },
            }
        ]

        try:
            response = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[
                    {
                        "role": "system",
                        "content": "You control a DALI lamp. Use minimal actions.",
                    },
                    {"role": "user", "content": text},
                ],
                tools=[{"type": "function", "function": functions[0]}],
            )

            call = response.choices[0].message.tool_calls
            if not call:
                return self._rules_plan(text)

            args = json.loads(call[0].function.arguments)
            return args.get("actions", [])

        except Exception as exc:
            logging.warning("LLM failed (%s); using rules.", exc)
            return self._rules_plan(text)

    # ---------- Rules fallback ----------
    def _rules_plan(self, text: str) -> List[Dict[str, Any]]:
        lowered = text.lower()
        actions: List[Dict[str, Any]] = []

        if "off" in lowered:
            actions.append({"action": "off"})

        if any(k in lowered for k in ["on", "restore", "resume"]):
            actions.append({"action": "on_last"})

        if any(k in lowered for k in ["warm", "yellow"]):
            actions.append({"action": "set_yellow"})
        elif any(k in lowered for k in ["cool", "white"]):
            actions.append({"action": "set_white"})

        m = re.search(r"(\d{1,3})\s*%", lowered)
        if m:
            actions.append(
                {"action": "set_brightness_pct", "pct": clamp(int(m.group(1)), 0, 100)}
            )

        return actions

    # ---------- Execution ----------
    def _rate_limit(self):
        now = time.monotonic()
        self._action_times = [t for t in self._action_times if now - t < 1.0]

        if len(self._action_times) >= MAX_ACTIONS_PER_SEC:
            time.sleep(1.0 - (now - self._action_times[0]))

    def _execute_action(self, action: str, params: Dict[str, Any]) -> None:
        self._rate_limit()

        logging.info("Executing action: %s %s", action, params)

        if action == "set_brightness_pct":
            self.lamp.set_brightness_pct(float(params.get("pct", 0)))
        elif action == "set_white":
            self.lamp.set_white()
        elif action == "set_yellow":
            self.lamp.set_yellow()
        elif action == "off":
            self.lamp.off()
        elif action == "on_last":
            self.lamp.on_last()

        save_state(self.lamp.state, self.state_path)
        self._action_times.append(time.monotonic())

    # ---------- Public ----------
    def handle_user_text(self, text: str, sensor_status: Optional[dict] = None) -> None:
        logging.info("User text: %s", text)

        lowered = text.lower().strip()

        if any(k in lowered for k in ["sensor", "occupancy", "presence", "status"]):
            print(f"sensor> {sensor_status or '(no data)'}")
            return

        for act in self._llm_plan(text):
            name = act.get("action")
            params = {k: v for k, v in act.items() if k != "action"}
            self._execute_action(name, params)
