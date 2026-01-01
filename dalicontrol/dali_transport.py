import hid
import time

VID = 0x17B5
PID = 0x0020

class DaliHidTransport:
    def __init__(self, vid=VID, pid=PID, pause=0.03):
        self.vid = vid
        self.pid = pid
        self.pause = pause
        self._counter = 1
        self.dev = None

    def open(self):
        self.dev = hid.device()
        self.dev.open(self.vid, self.pid)

    def close(self):
        if self.dev:
            self.dev.close()
            self.dev = None

    def _next_counter(self):
        self._counter = (self._counter + 1) & 0xFF
        if self._counter == 0:
            self._counter = 1
        return self._counter

    def _make_frame(self, b0: int, b1: int) -> bytes:
        b = bytearray(64)
        b[0] = 0x12
        b[1] = self._next_counter()
        b[2] = 0x00
        b[3] = 0x03
        b[4] = 0x00
        b[5] = 0x00
        b[6] = b0 & 0xFF
        b[7] = b1 & 0xFF
        return bytes(b)

    def send_dali16(self, b0: int, b1: int, pause=None):
        if pause is None:
            pause = self.pause
        frame64 = self._make_frame(b0, b1)
        self.dev.write(b"\x00" + frame64)
        time.sleep(pause)
