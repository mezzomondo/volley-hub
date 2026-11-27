import json
import os
from pathlib import Path


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"missing environment variable {name}")
    return value


DATABASE_URL = _env("DATABASE_URL", "postgresql://volleyhub@127.0.0.1:5433/volleyhub")
PUBLIC_URL = _env("PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
SIGNING_KEY = _env("SIGNING_KEY", "dev-only-signing-key").encode()
VIDEO_DIR = Path(_env("VIDEO_DIR", "/data/videos"))
CLIP_DIR = Path(_env("CLIP_DIR", "/data/clips"))
REPORT_DIR = Path(_env("REPORT_DIR", "/data/reports"))
DRAWING_DIR = Path(_env("DRAWING_DIR", "/data/drawings"))  # draw_drill output
LIBRARY_DIR = Path(_env("LIBRARY_DIR", "/data/library"))  # drill library files (vh library)
LOG_DIR = Path(_env("LOG_DIR", "/data/logs"))  # events/YYYY-MM-DD.jsonl, shipped to S3 hourly
TOOLS_DIR = Path(_env("TOOLS_DIR", "/data/tools"))  # the coach's report scripts (vh tools)
FONT_FILE = _env("FONT_FILE", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
# How long signed media links stay valid.
MEDIA_LINK_TTL_S = int(_env("MEDIA_LINK_TTL_S", str(7 * 24 * 3600)))
# Seconds to shift each skill's video time in clips, e.g. {"Serve": -1.5} when serves are recorded late.
CLIP_SKILL_OFFSETS = json.loads(_env("CLIP_SKILL_OFFSETS", "{}"))
