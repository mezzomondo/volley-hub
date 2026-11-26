"""Structured logging: one JSON object per event.

Each event goes to stdout (Docker keeps it with the other services' output) and is appended to
LOG_DIR/events/YYYY-MM-DD.jsonl, pure JSON lines, which the hourly job syncs to s3://BUCKET/logs/events/ and the
admin tools read with DuckDB. Never log tokens, access codes or signed-link query strings."""

import json
import sys
import threading
import time

from . import config

_lock = threading.Lock()


def event(kind: str, **fields) -> None:
    now = time.time()
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now * 1000) % 1000:03d}Z",
            "event": kind}
    line.update({k: v for k, v in fields.items() if v is not None})
    text = json.dumps(line, default=str, ensure_ascii=False) + "\n"
    sys.stdout.write(text)
    sys.stdout.flush()
    try:
        folder = config.LOG_DIR / "events"
        with _lock:
            folder.mkdir(parents=True, exist_ok=True)
            with open(folder / f"{line['ts'][:10]}.jsonl", "a", encoding="utf-8") as f:
                f.write(text)
    except OSError as e:  # logging must never break a request
        sys.stderr.write(f"log file not writable: {e}\n")


def short(value, limit: int = 500):
    """Keep logged arguments small (SQL, id lists)."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return value if len(text) <= limit else text[:limit] + "…"
