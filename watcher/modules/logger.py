import os
import sys
import traceback
from datetime import datetime
from termcolor import colored

_log_path: str = "./watcher.log"


def set_log_path(path: str):
    global _log_path
    _log_path = path


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write(line: str):
    try:
        with open(_log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def info(message: str):
    ts = _timestamp()
    print(f"[{colored(ts, 'cyan')}] [{colored('INFO', 'green')}] {message}")
    _write(f"[{ts}] [INFO] {message}")


def warn(message: str):
    ts = _timestamp()
    print(f"[{colored(ts, 'cyan')}] [{colored('WARN', 'yellow')}] {message}")
    _write(f"[{ts}] [WARN] {message}")


# Alias matching Python logging convention
warning = warn


def error(message: str):
    ts = _timestamp()
    print(f"[{colored(ts, 'cyan')}] [{colored('ERROR', 'red')}] {message}", file=sys.stderr)
    _write(f"[{ts}] [ERROR] {message}")


def exception(message: str):
    """Log an error message followed by the current exception traceback."""
    ts = _timestamp()
    tb = traceback.format_exc()
    full = f"{message}\n{tb.rstrip()}"
    print(f"[{colored(ts, 'cyan')}] [{colored('ERROR', 'red')}] {full}", file=sys.stderr)
    _write(f"[{ts}] [ERROR] {full}")


def debug(message: str):
    ts = _timestamp()
    _write(f"[{ts}] [DEBUG] {message}")
