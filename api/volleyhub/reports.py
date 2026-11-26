"""Reports: the coach's R scripts (season report, setter flow) and volleyreport's match report, rendered by the
reports worker. Same pattern as clips: create (deduplicated), poll, signed link."""

import datetime as dt
import hashlib
import json

from . import auth, config, db
from .auth import User
from .core import Forbidden, NotFound, _plain, _visible_matches_clause
from .analysis import _team

KINDS = {"season": ("pdf",), "match": ("pdf",), "setter_flow": ("pdf", "png")}
TOOL_FILES = ("season_lib.R", "season_report.R", "setter_flow.R")


def current_season(today: dt.date | None = None) -> str:
    d = today or dt.date.today()
    y = d.year if d.month >= 8 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def _season_window(season: str) -> tuple[dt.date, dt.date]:
    try:
        y = int(season[:4])
    except ValueError:
        raise ValueError("season looks like 2026-27")
    return dt.date(y, 8, 1), dt.date(y + 1, 7, 31)


def _tools_hash() -> str:
    h = hashlib.sha256()
    for name in TOOL_FILES:
        path = config.TOOLS_DIR / name
        if not path.is_file():
            raise ValueError(f"report scripts not installed on the server ({name} missing): run 'vh tools'")
        h.update(name.encode() + path.read_bytes())
    return h.hexdigest()


def _public(row: dict) -> dict:
    out = {k: row[k] for k in ("id", "kind", "season", "match_id", "format", "status", "error", "created_by",
                               "created_at", "finished_at")}
    out["team"] = row.get("team")
    if row["status"] == "ready" and row["storage_key"]:
        out["url"] = auth.signed_url(f"/media/reports/{row['storage_key']}")
    return _plain([out])[0]


def create_report(user: User, *, kind: str, team: str | None = None, season: str | None = None,
                  match_id: int | None = None, format: str = "pdf") -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if format not in KINDS[kind]:
        raise ValueError(f"{kind} reports come as {' or '.join(KINDS[kind])}")
    t = _team(team, default_ours=True)
    if not user.sees_all_teams and t["id"] not in user.team_ids:
        raise Forbidden("you can only make reports for your own team")
    vis, params = _visible_matches_clause(user)
    params["t"] = t["id"]
    where = [vis, "%(t)s in (m.home_team_id, m.visiting_team_id)"]
    if kind == "match":
        if not match_id:
            raise ValueError("match reports need match_id (see list_matches)")
        where.append("m.id = %(match_id)s")
        params["match_id"] = match_id
        season = None
    else:
        season = season or current_season()
        params["from"], params["to"] = _season_window(season)
        where.append("m.played_on between %(from)s and %(to)s")
        match_id = None
    matches = db.fetch_all(f"""
        select m.id, m.played_on, m.source_file, m.source_sha256, coalesce(m.competition, '') as competition,
               o.name as opponent
        from match m join team o on o.id = case when m.home_team_id = %(t)s then m.visiting_team_id
                                                else m.home_team_id end
        where {' and '.join(where)} order by m.played_on""", params)
    if not matches:
        raise NotFound(f"no matches for {t['name']}" + (f" in {season}" if season else ""))
    players = db.fetch_all("""
        select p.full_name as player, array_to_string(p.aliases, '|') as aliases, coalesce(sm.role, '') as role
        from squad_member sm join person p on p.id = sm.person_id
        where sm.team_id = %(t)s and (%(season)s::text is null or sm.season_id = %(season)s)
        order by p.full_name""", {"t": t["id"], "season": season})
    tools = _tools_hash() if kind != "match" else "volleyreport"
    spec = {
        "kind": kind, "format": format, "team": t["name"], "team_dir": t["name"].replace(" ", "_"),
        "season": season or current_season(matches[0]["played_on"]),
        "matches": [{"file": m["source_file"], "date": m["played_on"].isoformat(), "opponent": m["opponent"],
                     "competition": m["competition"], "include": "TRUE"} for m in matches],
        "players": players,
    }
    key = json.dumps({"kind": kind, "team": t["id"], "season": season, "match_id": match_id, "format": format,
                      "matches": [[m["id"], m["source_sha256"]] for m in matches], "tools": tools}, sort_keys=True)
    params_hash = hashlib.sha256(key.encode()).hexdigest()
    with db.conn() as c:
        row = c.execute("""
            insert into report (kind, team_id, season, match_id, format, params_hash, spec, created_by)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (params_hash) do update
              set status = case when report.status = 'failed' then 'queued' else report.status end,
                  error = case when report.status = 'failed' then null else report.error end
            returning *""", (kind, t["id"], season, match_id, format, params_hash, json.dumps(spec), user.name)).fetchone()
    row["team"] = t["name"]
    return _public(row)


def _visible(user: User, report_id: int) -> dict:
    row = db.fetch_one("select r.*, t.name as team from report r left join team t on t.id = r.team_id where r.id = %s",
                       (report_id,))
    if row is None or (not user.sees_all_teams and row["team_id"] not in user.team_ids):
        raise NotFound("report not found")
    return row


def get_report(user: User, report_id: int) -> dict:
    return _public(_visible(user, report_id))


def list_reports(user: User, mine: bool = False, limit: int = 20) -> list[dict]:
    where, params = ["true"], {"limit": max(1, min(int(limit), 200))}
    if not user.sees_all_teams:
        where.append("r.team_id = any(%(teams)s)")
        params["teams"] = list(user.team_ids)
    if mine:
        where.append("r.created_by = %(me)s")
        params["me"] = user.name
    rows = db.fetch_all(f"""select r.*, t.name as team from report r left join team t on t.id = r.team_id
                            where {' and '.join(where)} order by r.id desc limit %(limit)s""", params)
    return [_public(r) for r in rows]
