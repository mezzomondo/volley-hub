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
FONT_FILE = _env("FONT_FILE", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
# How long signed media links stay valid.
MEDIA_LINK_TTL_S = int(_env("MEDIA_LINK_TTL_S", str(7 * 24 * 3600)))
