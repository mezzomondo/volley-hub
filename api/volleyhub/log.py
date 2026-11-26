"""Structured logging: one JSON line per event on stdout. Docker keeps the lines, and an hourly job on the
server ships them to S3 (logs/). Never log tokens, access codes or signed-link query strings."""

import json
import sys
import time


def event(kind: str, **fields) -> None:
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z",
            "event": kind}
    line.update({k: v for k, v in fields.items() if v is not None})
    sys.stdout.write(json.dumps(line, default=str, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def short(value, limit: int = 500):
    """Keep logged arguments small (SQL, id lists)."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return value if len(text) <= limit else text[:limit] + "…"
