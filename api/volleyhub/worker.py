"""Clip renderer: takes queued clips from the database and renders them with ffmpeg.

    python -m volleyhub.worker
"""

import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path

from . import config, db

log = logging.getLogger("volleyhub.worker")
W, H, FPS = 1280, 720, 30


def _has_audio(path: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(out.stdout.strip())


def _duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def _caption_filter(caption_file: Path) -> str:
    font = config.FONT_FILE.replace(":", r"\:")
    path = str(caption_file).replace(":", r"\:")
    return (f"drawtext=fontfile={font}:textfile={path}:fontsize=26:fontcolor=white:"
            f"box=1:boxcolor=black@0.65:boxborderw=10:x=24:y=24")


def render_segment(seg: dict, out: Path, work: Path) -> None:
    src = config.VIDEO_DIR / seg["video"]
    if not src.is_file():
        raise FileNotFoundError(f"video not found: {seg['video']}")
    start, end = float(seg["start"]), float(seg["end"])
    dur = end - start
    caption = work / f"{out.stem}.txt"
    caption.write_text(seg.get("caption") or "")
    base = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS}"
    audio_in = "[0:a:0]" if _has_audio(src) else "[1:a]"
    inputs = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]
    if audio_in == "[1:a]":
        inputs += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    afmt = "aresample=48000,aformat=channel_layouts=stereo"

    slow = seg.get("slowmo")
    if slow:
        s1 = max(0.0, float(slow[0]) - start)
        s2 = min(dur, float(slow[1]) - start)
    if slow and 0 < s1 < s2 < dur:
        fc = (
            f"[0:v]{base},split=3[v1][v2][v3];"
            f"[v1]trim=0:{s1:.3f},setpts=PTS-STARTPTS[a];"
            f"[v2]trim={s1:.3f}:{s2:.3f},setpts=2*(PTS-STARTPTS)[b];"
            f"[v3]trim={s2:.3f}:{dur:.3f},setpts=PTS-STARTPTS[c];"
            f"{audio_in}{afmt},asplit=2[x1][x3];"
            f"[x1]atrim=0:{s1:.3f},asetpts=PTS-STARTPTS[xa];"
            f"anullsrc=r=48000:cl=stereo,atrim=0:{2 * (s2 - s1):.3f}[xb];"
            f"[x3]atrim={s2:.3f}:{dur:.3f},asetpts=PTS-STARTPTS[xc];"
            f"[a][xa][b][xb][c][xc]concat=n=3:v=1:a=1[cv][ca];"
            f"[cv]{_caption_filter(caption)},format=yuv420p[v]"
        )
    else:
        fc = f"[0:v]{base},{_caption_filter(caption)},format=yuv420p[v];{audio_in}{afmt}[ca]"
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-filter_complex", fc,
           "-map", "[v]", "-map", "[ca]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def render_clip(clip: dict) -> tuple[str, float]:
    spec = clip["spec"] if isinstance(clip["spec"], dict) else json.loads(clip["spec"])
    config.CLIP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{clip['id']:06d}.mp4"
    final = config.CLIP_DIR / name
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        parts = []
        for i, seg in enumerate(spec["segments"]):
            part = work / f"part{i:03d}.mp4"
            render_segment(seg, part, work)
            parts.append(part)
        if len(parts) == 1:
            parts[0].replace(final)
        else:
            listing = work / "parts.txt"
            listing.write_text("".join(f"file '{p}'\n" for p in parts))
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(final)],
                           check=True, capture_output=True, text=True)
    return name, _duration(final)


def claim_next() -> dict | None:
    with db.conn() as c:
        return c.execute("""
            update clip set status = 'rendering'
            where id = (select id from clip where status = 'queued' order by id
                        for update skip locked limit 1)
            returning *""").fetchone()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Clips left 'rendering' by a crash go back in the queue.
    with db.conn() as c:
        c.execute("update clip set status = 'queued' where status = 'rendering'")
    log.info("worker started")
    while True:
        clip = claim_next()
        if clip is None:
            time.sleep(2)
            continue
        log.info("rendering clip %s (%d segments)", clip["id"], len(clip["spec"]["segments"]))
        try:
            name, duration = render_clip(clip)
            with db.conn() as c:
                c.execute("update clip set status = 'ready', storage_key = %s, duration_s = %s, error = null "
                          "where id = %s", (name, round(duration, 2), clip["id"]))
            log.info("clip %s ready (%.1f s)", clip["id"], duration)
        except Exception as e:  # noqa: BLE001 - record any failure on the clip
            detail = getattr(e, "stderr", None) or str(e)
            log.error("clip %s failed: %s", clip["id"], detail)
            with db.conn() as c:
                c.execute("update clip set status = 'failed', error = %s where id = %s",
                          (str(detail)[-2000:], clip["id"]))


if __name__ == "__main__":
    main()
