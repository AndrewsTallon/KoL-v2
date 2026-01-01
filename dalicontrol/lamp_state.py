from dataclasses import dataclass
from typing import Optional, Tuple

from .dali_transport import DaliHidTransport
from .dali_controls import DaliControls, pct_to_level, clamp

# Your captured endpoints:
WARM_PRESET = (0x10, 0x27)  # "yellow"
COOL_PRESET = (0x32, 0x00)  # "white"

@dataclass
class LampState:
    # Last applied brightness as ARC level 0..254
    last_level: int = 254

    # Last applied DT8 temp as (DTR, DTR1)
    last_temp: Tuple[int, int] = COOL_PRESET

    # Track whether we think it's off
    is_off: bool = False


class LampController:
    """
    AI-facing controller: maintains "last set" state.
    Turn on => restores last brightness and keeps last temp (does not force warm/cool).
    """
    def __init__(self, controls: DaliControls, state: Optional[LampState] = None):
        self.ctrl = controls
        self.state = state or LampState()

    # -------- Brightness ----------
    def set_brightness_pct(self, pct: float):
        pct = clamp(float(pct), 0.0, 100.0)
        level = int(round((pct / 100.0) * 254))
        self.ctrl.set_arc_level(level)
        self.state.last_level = level
        self.state.is_off = (level == 0)

    def set_brightness_level(self, level: int):
        level = clamp(int(level), 0, 254)
        self.ctrl.set_arc_level(level)
        self.state.last_level = level
        self.state.is_off = (level == 0)

    # -------- Tunable white ----------
    def set_white(self):
        self.ctrl.dt8_set_temp_raw(*COOL_PRESET)
        self.state.last_temp = COOL_PRESET

    def set_yellow(self):
        self.ctrl.dt8_set_temp_raw(*WARM_PRESET)
        self.state.last_temp = WARM_PRESET

    def set_temp_raw(self, dtr: int, dtr1: int):
        dtr = clamp(int(dtr), 0, 255)
        dtr1 = clamp(int(dtr1), 0, 255)
        self.ctrl.dt8_set_temp_raw(dtr, dtr1)
        self.state.last_temp = (dtr, dtr1)

    # -------- Power ----------
    def off(self):
        self.ctrl.off()
        self.state.is_off = True

    def on_last(self):
        """
        Restore last-known state.
        Implementation: send temp (optional) then brightness.
        If you want the light to come back at exactly last level, this uses DIRECT ARC POWER.
        """
        # If you want, you can re-apply temp on power-on. It’s safe for your use case.
        dtr, dtr1 = self.state.last_temp
        self.ctrl.dt8_set_temp_raw(dtr, dtr1)

        # Restore last brightness; if last_level==0, choose a default (e.g., 50%)
        level = self.state.last_level
        if level == 0:
            level = 127  # default to ~50% if last was zero
        self.ctrl.set_arc_level(level)
        self.state.last_level = level
        self.state.is_off = False


if __name__ == "__main__":
    tx = DaliHidTransport(pause=0.05)
    tx.open()
    try:
        ctrl = DaliControls(tx)
        lamp = LampController(ctrl)

        # Example "AI script":
        lamp.set_brightness_pct(50)
        lamp.set_white()

        lamp.off()
        lamp.on_last()          # restores white + 50%

        lamp.set_yellow()
        lamp.set_brightness_pct(100)  # now "last set" = yellow + 100%

        lamp.off()
        lamp.on_last()          # restores yellow + 100%

    finally:
        tx.close()
