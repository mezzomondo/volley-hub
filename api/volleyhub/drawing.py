"""draw_drill: a volleyball court diagram (PNG) from a small JSON description written by the AI.

Coordinates are metres on a 9 x 18 court seen from above: x 0..9 left to right, y 0..18 from the near
baseline to the far baseline, net at y = 9. Points outside the court (servers, coaches, carts) are fine
(x -3..12, y -4..22). Instead of [x, y] any point can be a zone, "near:5" or "far:2": the centre of that
zone on that side (zones 1-6 as usual, each side seen by its own players facing the net)."""

import hashlib
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Circle, FancyArrowPatch, Rectangle, RegularPolygon  # noqa: E402

from . import auth, config  # noqa: E402
from .auth import User  # noqa: E402
from .core import Forbidden  # noqa: E402

TEAM_COLORS = {"A": "#1f5fbf", "B": "#c8372d", "coach": "#222222", "other": "#7a7a7a"}
ARROWS = {  # kind: (colour, line style, width)
    "ball": ("#e08a00", (0, (4, 3)), 2.2),
    "serve": ("#e08a00", (0, (1, 2)), 2.6),
    "attack": ("#c8372d", "-", 2.6),
    "move": ("#333333", "-", 1.8),
    "rotate": ("#6a3d9a", (0, (6, 3)), 1.8),
}
SPEC_HELP = (
    'spec = {"title": str, "view": "full"|"near"|"far", '
    '"players": [{"id": "R1", "label": "R", "team": "A"|"B"|"coach"|"other", "at": [x,y] | "near:5"}], '
    '"arrows": [{"from": id | [x,y] | "near:1", "to": ..., "kind": "ball"|"serve"|"attack"|"move"|"rotate", '
    '"label": str?, "curve": -0.5..0.5?}], '
    '"zones": [{"at": "far:6" | [x,y,w,h], "label": str?, "color": "#hex"?}], '
    '"objects": [{"type": "cone"|"target"|"box"|"cart", "at": ..., "label": str?}], '
    '"lines": [{"from": [x,y], "to": [x,y], "label": str?}] (dashed extra lines, e.g. a shortened court), '
    '"notes": [str]}. Metres: x 0..9, y 0..18 near to far baseline, net y=9; outside the court is fine.'
)


def _require_staff(user: User) -> None:
    if not user.is_staff:
        raise Forbidden("drawing drills is for staff")


def _zone_rect(side: str, zone: int) -> tuple[float, float, float, float]:
    """x, y, w, h of zone 1-6 on the near or far side."""
    col = {4: 0, 3: 1, 2: 2, 5: 0, 6: 1, 1: 2}[zone]
    front = zone in (2, 3, 4)
    if side == "near":
        return col * 3, (6 if front else 0), 3, (3 if front else 6)
    return (2 - col) * 3, (9 if front else 12), 3, (3 if front else 6)


def _point(p, ids: dict) -> tuple[float, float]:
    if isinstance(p, str) and p in ids:
        return ids[p]
    if isinstance(p, str) and ":" in p:
        side, zone = p.split(":", 1)
        if side not in ("near", "far") or not zone.strip().isdigit() or int(zone) not in range(1, 7):
            raise ValueError(f"bad zone {p!r}: use near:1..6 or far:1..6")
        x, y, w, h = _zone_rect(side, int(zone))
        return x + w / 2, y + h / 2
    if isinstance(p, (list, tuple)) and len(p) == 2:
        return float(p[0]), float(p[1])
    raise ValueError(f"bad point {p!r}: use [x, y], a zone like 'near:5' or a player id")


def render(spec: dict, out: Path) -> None:
    view = spec.get("view", "full")
    ylim = {"full": (-4, 22), "near": (-4, 11.5), "far": (6.5, 22)}.get(view)
    if ylim is None:
        raise ValueError("view must be full, near or far")
    fig, ax = plt.subplots(figsize=(6.4, 10 if view == "full" else 6.2), dpi=150)
    ax.set_xlim(-3.5, 12.5)
    ax.set_ylim(*ylim)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.patch.set_facecolor("white")

    ax.add_patch(Rectangle((0, 0), 9, 18, facecolor="#f3e3c3", edgecolor="#555555", linewidth=2, zorder=0))
    for y in (6, 12):
        ax.plot([0, 9], [y, y], color="#555555", linewidth=1.2, zorder=1)
    ax.plot([-0.6, 9.6], [9, 9], color="#111111", linewidth=4, zorder=2)

    for z in spec.get("zones", []):
        at = z.get("at")
        if isinstance(at, str):
            _point(at, {})  # validates
            side, zone = at.split(":", 1)
            x, y, w, h = _zone_rect(side, int(zone))
        elif isinstance(at, (list, tuple)) and len(at) == 4:
            x, y, w, h = map(float, at)
        else:
            raise ValueError(f"bad zone {at!r}: use 'far:6' or [x, y, w, h]")
        color = z.get("color", "#7fc97f")
        ax.add_patch(Rectangle((x, y), w, h, facecolor=color, alpha=0.35, edgecolor=color, linewidth=1.5, zorder=1))
        if z.get("label"):
            ax.text(x + w / 2, y + h / 2, z["label"], ha="center", va="center", fontsize=9, color="#333333", zorder=2)

    for ln in spec.get("lines", []):
        (x1, y1), (x2, y2) = _point(ln["from"], {}), _point(ln["to"], {})
        ax.plot([x1, x2], [y1, y2], color="#555555", linewidth=1.4, linestyle=(0, (5, 4)), zorder=1)
        if ln.get("label"):
            ax.text(max(x1, x2) + 0.2, (y1 + y2) / 2, ln["label"], fontsize=8, va="center", color="#444444")

    ids = {}
    for p in spec.get("players", []):
        if p.get("id"):
            ids[p["id"]] = _point(p["at"], {})

    for o in spec.get("objects", []):
        x, y = _point(o["at"], ids)
        kind = o.get("type", "cone")
        if kind == "cone":
            ax.add_patch(RegularPolygon((x, y), 3, radius=0.32, facecolor="#ff7f0e", edgecolor="#a04d00", zorder=3))
        elif kind == "target":
            for r, c in ((0.55, "#c8372d"), (0.35, "white"), (0.17, "#c8372d")):
                ax.add_patch(Circle((x, y), r, facecolor=c, edgecolor="#c8372d", zorder=3))
        elif kind in ("box", "cart"):
            ax.add_patch(Rectangle((x - 0.4, y - 0.3), 0.8, 0.6, facecolor="#8c6d46" if kind == "cart" else "#999999",
                                   edgecolor="#333333", zorder=3))
        else:
            raise ValueError(f"unknown object type {kind!r}: cone, target, box or cart")
        if o.get("label"):
            ax.text(x, y - 0.75, o["label"], ha="center", va="top", fontsize=7.5, color="#333333", zorder=4)

    for a in spec.get("arrows", []):
        kind = a.get("kind", "ball")
        if kind not in ARROWS:
            raise ValueError(f"arrow kind must be one of {', '.join(ARROWS)}")
        color, style, width = ARROWS[kind]
        (x1, y1), (x2, y2) = _point(a["from"], ids), _point(a["to"], ids)
        curve = max(-0.6, min(0.6, float(a.get("curve", 0))))
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), connectionstyle=f"arc3,rad={curve}", arrowstyle="-|>",
                                     mutation_scale=14, color=color, linewidth=width, linestyle=style,
                                     shrinkA=9, shrinkB=9, zorder=4))
        if a.get("label"):
            ax.text((x1 + x2) / 2 + 0.25, (y1 + y2) / 2, a["label"], fontsize=8, color=color, zorder=5,
                    bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.8, "pad": 1})

    for p in spec.get("players", []):
        x, y = ids.get(p.get("id")) or _point(p["at"], {})
        team = p.get("team", "A")
        color = TEAM_COLORS.get(team, TEAM_COLORS["other"])
        if team == "coach":
            ax.add_patch(Rectangle((x - 0.42, y - 0.42), 0.84, 0.84, facecolor=color, edgecolor="white", zorder=5))
        else:
            ax.add_patch(Circle((x, y), 0.45, facecolor=color, edgecolor="white", linewidth=1.5, zorder=5))
        ax.text(x, y, str(p.get("label", ""))[:3], ha="center", va="center", fontsize=8.5, color="white",
                fontweight="bold", zorder=6)

    if spec.get("title"):
        ax.set_title(str(spec["title"])[:80], fontsize=12, fontweight="bold", loc="left")
    notes = [str(n)[:140] for n in spec.get("notes", [])][:8]
    if notes:
        ax.text(-3.3, ylim[0] + 0.2, "\n".join(f"• {n}" for n in notes), fontsize=7.5, va="top", color="#333333",
                transform=ax.transData, clip_on=False)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def draw_drill(user: User, spec: dict) -> dict:
    _require_staff(user)
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object: " + SPEC_HELP)
    name = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:24] + ".png"
    out = config.DRAWING_DIR / name
    if not out.exists():
        try:
            render(spec, out)
        except (KeyError, TypeError, IndexError) as e:
            raise ValueError(f"bad spec ({e!r}). " + SPEC_HELP) from e
    return {"url": auth.signed_url(f"/media/drawings/{name}"), "file": name,
            "note": "the image is attached so you can check it before giving the user the url"}


def drawing_path(name: str) -> Path:
    return config.DRAWING_DIR / name
