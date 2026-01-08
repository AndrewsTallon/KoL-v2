import json
import importlib.util
import logging
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

from .sensor_usb import UsbOccupancyReader
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

        # --- occupancy automation ---
        self.occ_enabled = True
        self.occ_on_level_pct = 60          # brightness when turning on
        self.occ_off_delay_s = 20.0         # time CLEAR must persist before off
        self._last_presence_ts: float = 0.0
        self._last_occ_state: Optional[bool] = None  # True=present, False=clear

    def handle_occupancy(self, present: bool, ts: Optional[float] = None) -> None:
        """
        present=True => immediately ensure lamp is ON (optionally set brightness)
        present=False => do not turn off immediately; off happens after occ_off_delay_s
        """
        if not self.occ_enabled:
            return

        now = ts if ts is not None else time.monotonic()

        # track last seen presence time
        if present:
            self._last_presence_ts = now

        # edge logging (optional)
        if self._last_occ_state is None or self._last_occ_state != present:
            logging.info("Occupancy changed: %s", "PRESENT" if present else "CLEAR")
            self._last_occ_state = present

        # If PRESENT, turn on fast
        if present:
            # minimal: if currently off, restore last
            if self.lamp.state.is_off:
                self._execute_action("on_last", {})
            # optionally enforce a usable brightness level
            self._execute_action("set_brightness_pct", {"pct": self.occ_on_level_pct})
            return

        # If CLEAR, only turn off after delay (handled in tick)

    def tick(self) -> None:
        """
        Called periodically to apply off-delay behavior.
        If we haven't seen presence for occ_off_delay_s, turn off.
        """
        if not self.occ_enabled:
            return

        if self.lamp.state.is_off:
            return  # already off

        if self._last_presence_ts <= 0:
            return  # never saw presence yet

        now = time.monotonic()
        if (now - self._last_presence_ts) >= self.occ_off_delay_s:
            self._execute_action("off", {})

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
        except Exception as exc:  # pragma: no cover - defensive
            logging.warning("OpenAI not available, falling back to rules: %s", exc)
            self._openai_client = None
            return self._openai_client
        logging.info("OpenAI client initialized with model-based parser.")
        return self._openai_client

    def _llm_plan(self, text: str) -> List[Dict[str, Any]]:
        try:
            client = self._get_openai_client()
        except Exception as exc:  # pragma: no cover - defensive
            logging.warning("LLM setup failed (%s); using rules parser.", exc)
            return self._rules_plan(text)
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
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=[
                    {
                        "type": "function",
                        "function": functions[0],
                    }
                ],
                tool_choice="auto",
            )
            choice = response.choices[0].message
            tool_calls = choice.tool_calls or []
            if not tool_calls:
                logging.info("No function_call from model; using rules parser fallback.")
                return self._rules_plan(text)
            func_call = tool_calls[0].function
            if func_call.name != "set_actions":
                logging.info("Unexpected function %s; using rules parser.", func_call.name)
                return self._rules_plan(text)
            args = func_call.arguments or "{}"
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

        # [PATCH] Fix "bad default"
        if not actions:
            logging.info("Rules parser found no actions; doing nothing.")
            return []
            
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
    # [PATCH] Add sensor status query support
    def handle_user_text(self, text: str, sensor_status: Optional[dict] = None) -> None:
        logging.info("User text: %s", text)

        lowered = text.lower().strip()
        # Intercept sensor questions
        if any(k in lowered for k in ["sensor", "occupancy", "presence", "status"]) and (
            lowered.startswith("can you") or lowered.endswith("?")
        ):
            if sensor_status:
                logging.info("Sensor status: %s", sensor_status)
                print(f"sensor> {sensor_status}")
            else:
                print("sensor> (no sensor status available)")
            return

        actions = self._llm_plan(text)
        for act in actions:
            name = act.get("action")
            params = {k: v for k, v in act.items() if k != "action"}
            self._execute_action(name, params)


def run_ai_loop(dry_run: bool = False, sensor_port: Optional[str] = None, sensor_baud: int = 115200) -> None:
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

        # Start USB Sensor Reader
        usb_reader = None
        # Use a closure to hold the last known sensor state
        # Note: In a real system you might want thread-safety, but this single-threaded loop is fine.
        last_sensor_data = {}

        if sensor_port:
            def _on_evt(evt):
                nonlocal last_sensor_data
                # Update our local record for "status" queries
                last_sensor_data = {
                    "present": evt.present,
                    "mov_sig": evt.mov_sig,
                    "stat_sig": evt.stat_sig,
                    "ts": evt.ts
                }
                operator.handle_occupancy(evt.present, ts=evt.ts)

            usb_reader = UsbOccupancyReader(port=sensor_port, baud=sensor_baud, on_event=_on_evt)
            usb_reader.start()
            logging.info("USB occupancy reader running on %s @ %d", sensor_port, sensor_baud)
        else:
            logging.info("No sensor port provided; occupancy automation disabled (text commands only).")

        logging.info("AI operator ready. Type commands (Ctrl-D to exit). Dry-run=%s", dry_run)

        while True:
            # apply occupancy off-delay logic
            operator.tick()

            # non-blocking-ish console input
            try:
                if os.name == "nt":
                    import msvcrt
                    if msvcrt.kbhit():
                        user_text = input("you> ").strip()
                    else:
                        time.sleep(0.1)
                        continue
                else:
                    user_text = input("you> ").strip()
            except EOFError:
                logging.info("Exiting.")
                break
            except KeyboardInterrupt:
                logging.info("Interrupted.")
                break

            if not user_text:
                continue
            
            # Pass the last known sensor data (if any) to the handler
            # Note: This accesses `last_sensor_data` which is updated by the reader thread. 
            # In Python simple dict assignment/read is usually atomic enough for this display purpose.
            operator.handle_user_text(user_text, sensor_status=last_sensor_data if sensor_port else None)

    finally:
        if 'usb_reader' in locals() and usb_reader:
            usb_reader.stop()
        if tx:
            try:
                tx.close()
            except Exception:  # pragma: no cover - defensive
                pass