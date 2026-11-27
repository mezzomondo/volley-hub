"""Turn a season vault (Markdown, as built from the Noteful exports) into the bundle the server imports.

    python3 -I scripts/library-bundle.py "~/Documents/Coaching/Season 2025-26" > bundle.json

Vault layout:
    Sessions/<Weekday ...>/<YYYY-MM-DD[ title]>.md  (+ .pdf, img/*.png)
        "## <heading>" sections; a drill section has the line "Drill: [Name](../../Drills/<slug>.md) (Theme)"
    Drills/<slug>.md
        "# Name", "Theme: ...", then one "## <date>, <group>" block per use with "Time: ...", a summary paragraph,
        diagrams and "Session: [..](../Sessions/<folder>/<file>.md)"

Privacy: attendance is not published. The "Players:" line becomes a head count and every player pseudonym
(PlayerN) becomes "X", which keeps group sizes and lineups readable without names."""

import json
import re
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path

PLAYER = re.compile(r"\bPlayer\d+\b(\s*\[\?\])?\*?")
DRILL_LINE = re.compile(r"^Drill: \[(?P<name>[^\]]+)\]\(\.\./\.\./Drills/(?P<slug>[^)]+)\.md\)(?: \((?P<theme>[^)]*)\))?\s*$", re.M)
IMAGE = re.compile(r"!\[[^\]]*\]\((?P<path>[^)]+)\)")


def anonymise(text: str) -> str:
    return PLAYER.sub("X", text)


def group_of(folder: str) -> str:
    """'Wednesday Coyotes' -> 'Wednesday': sessions are grouped by training day only."""
    return folder.split()[0]


def images_with_captions(body: str, base: str) -> list[dict]:
    out = []
    lines = body.splitlines()
    for i, line in enumerate(lines):
        for m in IMAGE.finditer(line):
            path = urllib.parse.unquote(m.group("path"))
            caption = lines[i + 1].strip() if i + 1 < len(lines) and lines[i + 1].strip() and not IMAGE.search(lines[i + 1]) else None
            out.append({"path": f"{base}/{path}", "caption": anonymise(caption) if caption else None})
    return out


def parse_drills(vault: Path) -> tuple[dict, dict]:
    drills, uses = {}, defaultdict(list)   # uses[(session file, slug)] = [{duration, summary}, ...] in order
    for f in sorted((vault / "Drills").glob("*.md")):
        if f.name == "README.md":
            continue
        text = f.read_text()
        name = re.search(r"^# (.+)$", text, re.M).group(1).strip()
        theme = (re.search(r"^Theme: (.+)$", text, re.M) or [None, None])[1]
        drills[f.stem] = {"slug": f.stem, "name": name, "theme": theme and theme.strip()}
        for block in re.split(r"^## ", text, flags=re.M)[1:]:
            session = re.search(r"^Session: \[[^\]]*\]\(\.\./(?P<p>[^)]+)\)", block, re.M)
            if not session:
                continue
            duration = re.search(r"^Time: (.+)$", block, re.M)
            summary = [l.strip() for l in block.splitlines()[1:]
                       if l.strip() and not re.match(r"^(Time: |Session: |!\[)", l.strip())]
            uses[(urllib.parse.unquote(session.group("p")), f.stem)].append(
                {"duration": duration.group(1).strip() if duration else None,
                 "summary": anonymise(" ".join(summary)) or None})
    return drills, uses


def parse_sessions(vault: Path, uses: dict) -> list[dict]:
    sessions = []
    for f in sorted((vault / "Sessions").glob("*/*.md")):
        rel = f.relative_to(vault).as_posix()
        folder = f.parent.name
        date = re.match(r"\d{4}-\d{2}-\d{2}", f.stem)
        if not date:
            continue
        text = f.read_text()
        head, *blocks = re.split(r"^## ", text, flags=re.M)
        players = re.search(r"^Players: (.+)$", head, re.M)
        count = len(set(re.findall(r"Player\d+", players.group(1)))) if players else None
        intro = "\n".join(l for l in head.splitlines()[1:]
                          if not l.startswith(("Players:", "Drills in this session:"))).strip()
        rest = f.stem[len(date.group(0)):].strip()
        group = group_of(folder)
        seen = defaultdict(int)
        sections = []
        for i, block in enumerate(blocks):
            heading, _, body = block.partition("\n")
            m = DRILL_LINE.search(body)
            slug = m.group("slug") if m else None
            body = DRILL_LINE.sub("", body).strip()
            use = {}
            if slug:
                k = (rel, slug)
                lst = uses.get(k, [])
                use = lst[seen[k]] if seen[k] < len(lst) else {}
                seen[k] += 1
            sections.append({
                "ord": i + 1, "heading": anonymise(heading.strip()), "body_md": anonymise(body),
                "drill": slug, "duration": use.get("duration"), "summary": use.get("summary"),
                "images": images_with_captions(body, f"Sessions/{folder}"),
            })
        pdf = f.with_suffix(".pdf")
        sessions.append({
            "key": rel[:-3], "date": date.group(0), "group": group,
            "title": f"{date.group(0)} {group}" + (f" {rest}" if rest else ""),
            "kind": "feedback" if "feedback" in rest.lower() else "session",
            "players": count, "intro_md": anonymise(intro) or None,
            "pdf": pdf.relative_to(vault).as_posix() if pdf.exists() else None,
            "sections": sections,
        })
    return sessions


def main() -> None:
    vault = Path(sys.argv[1]).expanduser()
    season = re.search(r"\d{4}-\d{2}", vault.name)
    if not season:
        sys.exit("the vault folder name must contain the season, e.g. 'Season 2025-26'")
    drills, uses = parse_drills(vault)
    sessions = parse_sessions(vault, uses)
    used = {s["drill"] for x in sessions for s in x["sections"] if s["drill"]}
    missing = used - drills.keys()
    if missing:
        sys.exit(f"sessions link drills with no page in Drills/: {', '.join(sorted(missing))}")
    leaks = [x["key"] for x in sessions if re.search(r"Player\d+", json.dumps(x))]
    if leaks:
        sys.exit(f"player names left in: {leaks}")
    json.dump({"version": 1, "season": season.group(0), "drills": sorted(drills.values(), key=lambda d: d["slug"]),
               "sessions": sessions}, sys.stdout, ensure_ascii=False, indent=1)
    print(f"{len(drills)} drills, {len(sessions)} sessions, {sum(len(x['sections']) for x in sessions)} sections, "
          f"{sum(1 for x in sessions for s in x['sections'] if s['drill'])} drill uses", file=sys.stderr)


if __name__ == "__main__":
    main()
