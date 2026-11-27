"""Drill library and training sessions: import (from scripts/library-bundle.py) and staff-only reads."""

import json
from pathlib import Path

from . import auth, config, db
from .auth import User
from .core import Forbidden, NotFound, _plain


def _require_staff(user: User) -> None:
    if not user.is_staff:
        raise Forbidden("the drill library is for staff")


# ------------------------------------------------------------------ import

def import_bundle(path: Path) -> str:
    b = json.loads(Path(path).read_text())
    if b.get("version") != 1:
        raise ValueError("unknown bundle version")
    season = b["season"]
    with db.conn() as c:
        ids = {}
        for d in b["drills"]:
            ids[d["slug"]] = c.execute("""
                insert into library_drill (slug, name, theme) values (%s, %s, %s)
                on conflict (slug) do update set name = excluded.name, theme = excluded.theme, updated_at = now()
                returning id""", (d["slug"], d["name"], d.get("theme"))).fetchone()["id"]
        c.execute("delete from library_session where season = %s", (season,))
        n = 0
        for s in b["sessions"]:
            sid = c.execute("""
                insert into library_session (key, season, grp, date, title, kind, players, intro_md, pdf_path)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
                (f"{season}/{s['key']}", season, s["group"], s["date"], s["title"], s.get("kind", "session"),
                 s.get("players"), s.get("intro_md"), s.get("pdf") and f"{season}/{s['pdf']}")).fetchone()["id"]
            for x in s["sections"]:
                images = [{"path": f"{season}/{i['path']}", "caption": i.get("caption")} for i in x.get("images", [])]
                c.execute("""
                    insert into library_section (session_id, ord, heading, body_md, drill_id, duration, summary, images)
                    values (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (sid, x["ord"], x["heading"], x.get("body_md") or "", ids.get(x.get("drill")),
                     x.get("duration"), x.get("summary"), json.dumps(images)))
                n += 1
        gone = c.execute("""delete from library_drill d where not exists
                            (select 1 from library_section s where s.drill_id = d.id) returning slug""").fetchall()
    return (f"season {season}: {len(b['drills'])} drills, {len(b['sessions'])} sessions, {n} sections"
            + (f"; removed {len(gone)} drills no longer used" if gone else ""))


# ------------------------------------------------------------------ reads

def _image_files(images: list[dict]) -> list[dict]:
    """Published diagrams with a signed link; images not on the server are left out."""
    out = []
    for i in images:
        if (config.LIBRARY_DIR / i["path"]).is_file():
            out.append({"url": auth.signed_url(f"/media/library/{i['path']}"), "caption": i.get("caption"),
                        "path": i["path"]})
    return out


def list_drills(user: User, theme: str | None = None) -> list[dict]:
    _require_staff(user)
    return _plain(db.fetch_all("""
        select d.id, d.name, d.theme, count(s.id) as uses, max(ss.date) as last_used,
               string_agg(distinct ss.grp, ', ') as groups
        from library_drill d join library_section s on s.drill_id = d.id join library_session ss on ss.id = s.session_id
        where %(theme)s::text is null or d.theme ilike %(theme)s
        group by d.id order by d.theme, d.name""", {"theme": theme}))


def search_drills(user: User, text: str | None = None, theme: str | None = None, group: str | None = None,
                  season: str | None = None, limit: int = 20) -> list[dict]:
    """Drills ranked by how well their uses match the text (English stemming), with the best-matching use."""
    _require_staff(user)
    rows = db.fetch_all("""
        with hits as (
            select s.*, ss.date, ss.grp, ss.season, ss.title as session,
                   case when %(q)s::text is null then 0
                        else ts_rank_cd(s.search, to_tsquery('english', replace(plainto_tsquery('english', %(q)s)::text, ' & ', ' | ')))
                             + case when d.name ilike '%%' || %(q)s || '%%' then 1 else 0 end end as rank
            from library_section s join library_session ss on ss.id = s.session_id
            join library_drill d on d.id = s.drill_id
            where (%(q)s::text is null or s.search @@ to_tsquery('english', replace(plainto_tsquery('english', %(q)s)::text, ' & ', ' | '))
                   or d.name ilike '%%' || %(q)s || '%%')
              and (%(theme)s::text is null or d.theme ilike %(theme)s)
              and (%(grp)s::text is null or ss.grp ilike %(grp)s)
              and (%(season)s::text is null or ss.season = %(season)s)
        )
        select distinct on (d.id) d.id, d.name, d.theme,
               (select count(*) from library_section x where x.drill_id = d.id) as uses,
               h.rank, h.summary, h.duration, h.date as example_date, h.grp as example_group, h.session_id
        from hits h join library_drill d on d.id = h.drill_id
        order by d.id, h.rank desc, h.date desc""",
        {"q": text or None, "theme": theme, "grp": group, "season": season})
    rows = sorted(rows, key=lambda r: (-(r["rank"] or 0), -r["uses"], r["name"]))[:max(1, min(limit, 100))]
    for r in rows:
        r.pop("rank", None)
    return _plain(rows)


def _drill_row(ref: str | int) -> dict:
    if isinstance(ref, int) or str(ref).isdigit():
        row = db.fetch_one("select * from library_drill where id = %s", (int(ref),))
    else:
        row = (db.fetch_one("select * from library_drill where slug = %s or name ilike %s", (ref, ref))
               or db.fetch_one("select * from library_drill where name ilike %s order by length(name) limit 1",
                               (f"%{ref}%",)))
    if row is None:
        raise NotFound(f"no drill matching {ref!r}")
    return row


def get_drill(user: User, drill: str) -> dict:
    _require_staff(user)
    d = _drill_row(drill)
    uses = db.fetch_all("""
        select ss.id as session_id, ss.title as session, ss.date, ss.grp as "group", s.heading, s.duration,
               s.summary, s.body_md, s.images
        from library_section s join library_session ss on ss.id = s.session_id
        where s.drill_id = %s order by ss.date""", (d["id"],))
    for u in uses:
        u["diagrams"] = _image_files(u.pop("images") or [])
    return {"id": d["id"], "name": d["name"], "theme": d["theme"], "uses": _plain(uses)}


def list_sessions(user: User, group: str | None = None, season: str | None = None, date_from: str | None = None,
                  date_to: str | None = None, drill: str | None = None) -> list[dict]:
    _require_staff(user)
    drill_id = _drill_row(drill)["id"] if drill else None
    return _plain(db.fetch_all("""
        select ss.id, ss.title, ss.date, ss.grp as "group", ss.season, ss.kind, ss.players,
               string_agg(d.name, ' | ' order by s.ord) filter (where d.id is not null) as drills
        from library_session ss left join library_section s on s.session_id = ss.id
        left join library_drill d on d.id = s.drill_id
        where (%(grp)s::text is null or ss.grp ilike %(grp)s) and (%(season)s::text is null or ss.season = %(season)s)
          and (%(df)s::date is null or ss.date >= %(df)s) and (%(dt)s::date is null or ss.date <= %(dt)s)
          and (%(drill)s::int is null or exists (select 1 from library_section x
                                                 where x.session_id = ss.id and x.drill_id = %(drill)s))
        group by ss.id order by ss.date""",
        {"grp": group, "season": season, "df": date_from, "dt": date_to, "drill": drill_id}))


def get_session(user: User, session_id: int) -> dict:
    _require_staff(user)
    s = db.fetch_one("select * from library_session where id = %s", (session_id,))
    if s is None:
        raise NotFound(f"no session {session_id}")
    sections = db.fetch_all("""
        select s.ord, s.heading, d.name as drill, d.theme, s.duration, s.summary, s.body_md, s.images
        from library_section s left join library_drill d on d.id = s.drill_id
        where s.session_id = %s order by s.ord""", (session_id,))
    for x in sections:
        x["diagrams"] = _image_files(x.pop("images") or [])
    pdf = s["pdf_path"] if s["pdf_path"] and (config.LIBRARY_DIR / s["pdf_path"]).is_file() else None
    return {"id": s["id"], "title": s["title"], "date": s["date"].isoformat(), "group": s["grp"],
            "season": s["season"], "kind": s["kind"], "players": s["players"], "intro": s["intro_md"],
            "pdf": auth.signed_url(f"/media/library/{pdf}") if pdf else None, "sections": _plain(sections)}


def image_paths(result: dict, limit: int) -> list[Path]:
    """Local files of the diagrams in a get_drill / get_session result, for inline MCP images."""
    items = result.get("uses") or result.get("sections") or []
    paths = [config.LIBRARY_DIR / d["path"] for it in items for d in it.get("diagrams", [])]
    return paths[:limit]
