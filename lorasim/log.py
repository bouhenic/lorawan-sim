"""Journal coloré dans le terminal."""

import sys
import time

from . import events

COLORS = {
    "info": "", "ok": "\033[32m", "warn": "\033[33m",
    "error": "\033[31m", "down": "\033[36m",
}
KIND = {"gw": "\033[35m", "dev": "\033[34m", "sys": "\033[90m"}
RESET = "\033[0m"
USE_COLOR = sys.stdout.isatty()


def log(source: str, msg: str, level: str = "info", kind: str = "sys"):
    events.publish({"ts": time.time(), "source": source, "msg": msg, "level": level, "kind": kind})
    ts = time.strftime("%H:%M:%S")
    if USE_COLOR:
        print(f"\033[90m{ts}{RESET} {KIND.get(kind, '')}{source:<14}{RESET} "
              f"{COLORS.get(level, '')}{msg}{RESET}", flush=True)
    else:
        print(f"{ts} {source:<14} {msg}", flush=True)
