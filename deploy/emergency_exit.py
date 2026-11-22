"""Emergency-exit input -> orderly shutdown. NOT A SAFETY FUNCTION.

Review 7.3, decision D8: the input is GPIO17 (header pin 11) on the CM5, from an ISO1211
isolator. Voltage present = healthy. When the voltage has been gone for 200 ms, the
application is stopped (it finishes pending writes and closes the database), then the CM5
powers off. Installed only with install.sh --emergency-exit, once the input is wired.

Uses libgpiod v2 (Raspberry Pi OS package python3-libgpiod). NOT VERIFIED on hardware:
there is no interface board yet.
"""

import subprocess
import sys
import time

import gpiod
from gpiod.line import Bias, Direction, Value

CHIP, LINE, HOLD_S = "/dev/gpiochip0", 17, 0.2


def main() -> int:
    with gpiod.request_lines(CHIP, consumer="eol-exit",
                             config={LINE: gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.DISABLED)}) as req:
        low_since = None
        while True:
            healthy = req.get_value(LINE) == Value.ACTIVE
            now = time.monotonic()
            if healthy:
                low_since = None
            elif low_since is None:
                low_since = now
            elif now - low_since >= HOLD_S:
                print("emergency exit: stopping the application, then powering off", flush=True)
                subprocess.run(["systemctl", "stop", "eol-app.service"], timeout=10)
                subprocess.run(["systemctl", "poweroff"])
                return 0
            time.sleep(0.02)


if __name__ == "__main__":
    sys.exit(main())
