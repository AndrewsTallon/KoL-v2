import json
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

STATE_PATH = Path(__file__).with_name("state.json")
MAX_ACTIONS_PER_SEC = 4


class NullControls:
    """No-op controls for dry runs; mirrors the LampController expectations."""

    def off(self):
        logging.info("[dry-run] off()")

    def set_arc_level(self, level: int):
        logging.info("[dry-run] set_arc_level(%s)", level)

    def dt8_set_temp_raw(self, dtr: int, dtr1: int):
        logging.info("[dry-run] dt8_set_temp_raw(%s, %s)", dtr, dtr1)


def load_state(path: Path = STATE_PATH) -> LampState:
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
            return LampState(
                last_level=int(payload.get("last_level", 254)),
                last_temp=tuple(payload.get("last_temp", COOL_PRESET)),  # type: ignore[arg-type]
                is_off=bool(payload.get("is_off", False)),
            )
    except FileNotFoundError:
        return LampState()
    except Exception as exc:  # pragma: no cover - defensive
        logging.warning("Failed to load state file %s: %s", path, exc)
        return LampState()


def save_state(state: LampState, path: Path = STATE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(asdict(state), f, indent=2)
    except Exception as exc:  # pragma: no cover - defensive
        logging.error("Failed to persist state to %s: %s", path, exc)


class AIOperator:
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
        try:
            import openai  # type: ignore

            openai.api_key = api_key
            self._openai_client = openai
            logging.info("OpenAI client initialized with model-based parser.")
        except Exception as exc:  # pragma: no cover - defensive
            logging.warning("OpenAI not available, falling back to rules: %s", exc)
            self._openai_client = None
        return self._openai_client

    def _llm_plan(self, text: str) -> List[Dict[str, Any]]:
        client = self._get_openai_client()
        if client is None:
            return self._rules_plan(text)

        functions = [
            {
                "name": "set_actions",
                "description": "Choose the sequence of lighting actions to satisfy the user request.",
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
                                    "pct": {"type": "number", "minimum": 0, "maximum": 100},
                                },
                                "required": ["action"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["actions"],
                    "additionalProperties": False,
                },
            }
        ]

        messages = [
            {
                "role": "system",
                "content": (
                    "You control a DALI lamp. Use only the provided function to return actions. "
                    "Prefer minimal sequences. To turn the lamp on use 'on_last'. "
                    "Use set_brightness_pct for brightness changes."
                ),
            },
            {"role": "user", "content": text},
        ]

        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        try:
            response = client.ChatCompletion.create(  # type: ignore[attr-defined]
                model=model,
                messages=messages,
                functions=functions,
                function_call="auto",
            )
            choice = response["choices"][0]["message"]
            func_call = choice.get("function_call")
            if not func_call:
                logging.info("No function_call from model; using rules parser fallback.")
                return self._rules_plan(text)
            if func_call.get("name") != "set_actions":
                logging.info("Unexpected function %s; using rules parser.", func_call.get("name"))
                return self._rules_plan(text)
            args = func_call.get("arguments") or "{}"
            parsed = json.loads(args)
            actions = parsed.get("actions", [])
            logging.info("LLM proposed actions: %s", actions)
            return actions
        except Exception as exc:  # pragma: no cover - defensive
            logging.warning("LLM parsing failed (%s); using rules fallback.", exc)
            return self._rules_plan(text)

    # ---------- Rules fallback ----------
    def _rules_plan(self, text: str) -> List[Dict[str, Any]]:
        lowered = text.lower()
        actions: List[Dict[str, Any]] = []

        if "off" in lowered:
            actions.append({"action": "off"})

        if any(tok in lowered for tok in ["on", "turn on", "restore", "resume"]):
            actions.append({"action": "on_last"})

        if any(tok in lowered for tok in ["warm", "yellow"]):
            actions.append({"action": "set_yellow"})
        elif any(tok in lowered for tok in ["cool", "white"]):
            actions.append({"action": "set_white"})

        pct_match = re.search(r"(\d{1,3})\s*%", lowered)
        if pct_match:
            pct_val = clamp(int(pct_match.group(1)), 0, 100)
            actions.append({"action": "set_brightness_pct", "pct": pct_val})

        if not actions:
            logging.info("Rules parser found no actions; defaulting to on_last.")
            actions.append({"action": "on_last"})
        logging.info("Rules proposed actions: %s", actions)
        return actions

    # ---------- Execution ----------
    def _rate_limit(self):
        now = time.monotonic()
        self._action_times = [t for t in self._action_times if now - t < 1.0]
        if len(self._action_times) >= MAX_ACTIONS_PER_SEC:
            sleep_for = 1.0 - (now - self._action_times[0])
            if sleep_for > 0:
                logging.info("Rate limiting: sleeping %.3fs", sleep_for)
                time.sleep(sleep_for)
        self._action_times = [t for t in self._action_times if now - t < 1.0]

    def _is_redundant(self, action: str, params: Dict[str, Any]) -> bool:
        state = self.lamp.state
        if action == "set_white":
            return state.last_temp == COOL_PRESET
        if action == "set_yellow":
            return state.last_temp == WARM_PRESET
        if action == "off":
            return state.is_off
        if action == "on_last":
            return not state.is_off
        if action == "set_brightness_pct":
            pct = clamp(float(params.get("pct", 0.0)), 0.0, 100.0)
            level = int(round((pct / 100.0) * 254))
            return state.last_level == level and not state.is_off
        return False

    def _execute_action(self, action: str, params: Dict[str, Any]) -> None:
        if self._is_redundant(action, params):
            logging.info("Skipping redundant action %s with %s", action, params)
            return

        self._rate_limit()

        logging.info("Executing action: %s %s", action, params)
        if action == "set_brightness_pct":
            pct = clamp(float(params.get("pct", 0.0)), 0.0, 100.0)
            self.lamp.set_brightness_pct(pct)
        elif action == "set_white":
            self.lamp.set_white()
        elif action == "set_yellow":
            self.lamp.set_yellow()
        elif action == "off":
            self.lamp.off()
        elif action == "on_last":
            self.lamp.on_last()
        else:
            logging.info("Unknown action %s ignored.", action)
            return

        save_state(self.lamp.state, self.state_path)
        self._action_times.append(time.monotonic())
        logging.info(
            "Updated state: level=%s temp=%s off=%s",
            self.lamp.state.last_level,
            self.lamp.state.last_temp,
            self.lamp.state.is_off,
        )

    # ---------- Public ----------
    def handle_user_text(self, text: str) -> None:
        logging.info("User text: %s", text)
        actions = self._llm_plan(text)
        for act in actions:
            name = act.get("action")
            params = {k: v for k, v in act.items() if k != "action"}
            self._execute_action(name, params)


def run_ai_loop(dry_run: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = load_state()
    tx: Optional[DaliHidTransport] = None

    try:
        if dry_run:
            controls = NullControls()
        else:
            tx = DaliHidTransport()
            tx.open()
            controls = DaliControls(tx)

        lamp = LampController(controls, state)
        operator = AIOperator(lamp, dry_run=dry_run)

        logging.info("AI operator ready. Type commands (Ctrl-D to exit). Dry-run=%s", dry_run)
        while True:
            try:
                user_text = input("you> ").strip()
            except EOFError:
                logging.info("Exiting.")
                break
            except KeyboardInterrupt:
                logging.info("Interrupted.")
                break
            if not user_text:
                continue
            operator.handle_user_text(user_text)
    finally:
        if tx:
            try:
                tx.close()
            except Exception:  # pragma: no cover - defensive
                pass
