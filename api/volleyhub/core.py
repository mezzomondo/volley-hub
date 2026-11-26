"""Functions shared by REST and MCP. Every function takes the calling user and
only returns what that user may see."""

import hashlib
import json
from decimal import Decimal

from psycopg import sql as pgsql

from . import auth, db
from .auth import User

SKILLS = ("Serve", "Reception", "Set", "Attack", "Block", "Dig", "Freeball")
MAX_ROWS = 500


class Forbidden(Exception):
    pass


class NotFound(Exception):
    pass


def _visible_matches_clause(user: User, alias: str = "m") -> tuple[str, dict]:
    if user.sees_all_teams:
        return "true", {}
    return (f"({alias}.home_team_id = any(%(user_teams)s) or {alias}.visiting_team_id = any(%(user_teams)s))",
            {"user_teams": list(user.team_ids)})


def _plain(rows: list[dict]) -> list[dict]:
    """JSON-friendly values."""
    out = []
    for r in rows:
        out.append({k: (float(v) if isinstance(v, Decimal) else v.isoformat() if hasattr(v, "isoformat") else v)
                    for k, v in r.items()})
    return out


def watch_url(match_id: int, start: float, end: float) -> str:
    return auth.signed_url(f"/watch/{match_id}", {"t": round(start, 1), "e": round(end, 1)})


# ------------------------------------------------------------------ catalogue

def list_teams(user: User) -> list[dict]:
    return db.fetch_all("select id, name, is_ours, gender, league from team order by is_ours desc, name")


def list_matches(user: User, team: str | None = None) -> list[dict]:
    vis, params = _visible_matches_clause(user)
    where = [vis]
    if team:
        where.append("(h.name ilike %(team)s or v.name ilike %(team)s)")
        params["team"] = f"%{team}%"
    rows = db.fetch_all(f"""
        select m.id, m.played_on, m.competition, h.name as home, v.name as visiting,
               m.home_sets, m.visiting_sets, m.set_scores,
               exists (select 1 from video where match_id = m.id) as has_video
        from match m join team h on h.id = m.home_team_id join team v on v.id = m.visiting_team_id
        where {' and '.join(where)}
        order by m.played_on""", params)
    return _plain(rows)


def list_players(user: User, team: str | None = None) -> list[dict]:
    """Squad players of our teams (and, with team=opponent name, players seen against us)."""
    vis, params = _visible_matches_clause(user)
    params["team"] = f"%{team}%" if team else "%"
    rows = db.fetch_all(f"""
        select p.id, p.full_name, t.name as team,
               (select role from squad_member s where s.person_id = p.id and s.team_id = t.id
                order by season_id desc limit 1) as role,
               array_agg(distinct mp.shirt_number) as shirt_numbers,
               count(distinct mp.match_id) as matches
        from match_player mp
        join person p on p.id = mp.person_id
        join team t on t.id = mp.team_id
        join match m on m.id = mp.match_id
        where {vis} and t.name ilike %(team)s
        group by p.id, p.full_name, t.id, t.name
        order by t.name, p.full_name""", params)
    return _plain(rows)


def list_tags(user: User, match_id: int | None = None) -> list[dict]:
    vis, params = _visible_matches_clause(user)
    where = [vis]
    if match_id:
        where.append("m.id = %(match_id)s")
        params["match_id"] = match_id
    rows = db.fetch_all(f"""
        select g.id, g.match_id, m.played_on, g.set_number, g.label, g.video_time_s
        from tag g join match m on m.id = g.match_id
        where {' and '.join(where)} order by m.played_on, g.video_time_s""", params)
    for r in rows:
        if r["video_time_s"] is not None:
            t = float(r["video_time_s"])
            r["watch_url"] = watch_url(r["match_id"], max(0, t - 4), t + 4)
    return _plain(rows)


# ------------------------------------------------------------------ actions

def search_actions(user: User, *, player: str | None = None, team: str | None = None,
                   opponent: str | None = None, match_id: int | None = None, skill: str | None = None,
                   evaluation: list[str] | None = None, skill_type: str | None = None,
                   attack_code: str | None = None, set_number: int | None = None,
                   phase: str | None = None, limit: int = 100) -> list[dict]:
    vis, params = _visible_matches_clause(user)
    where = [vis]
    if player:
        where.append("(p.full_name ilike %(player)s or %(player_exact)s ilike any(p.aliases))")
        params.update(player=f"%{player}%", player_exact=player)
    if team:
        where.append("t.name ilike %(team)s")
        params["team"] = f"%{team}%"
    if opponent:
        where.append("""exists (select 1 from team o where o.id in (m.home_team_id, m.visiting_team_id)
                        and o.id <> a.team_id and o.name ilike %(opponent)s)""")
        params["opponent"] = f"%{opponent}%"
    if match_id:
        where.append("a.match_id = %(match_id)s")
        params["match_id"] = match_id
    if skill:
        canonical = next((s for s in SKILLS if s.lower() == skill.lower()), None)
        if canonical is None:
            raise ValueError(f"skill must be one of {', '.join(SKILLS)}")
        where.append("a.skill = %(skill)s")
        params["skill"] = canonical
    if evaluation:
        where.append("a.evaluation_code = any(%(evaluation)s)")
        params["evaluation"] = list(evaluation)
    if skill_type:
        where.append("a.skill_type ilike %(skill_type)s")
        params["skill_type"] = f"%{skill_type}%"
    if attack_code:
        where.append("a.attack_code = %(attack_code)s")
        params["attack_code"] = attack_code.upper()
    if set_number:
        where.append("a.set_number = %(set_number)s")
        params["set_number"] = set_number
    if phase:
        where.append("a.phase ilike %(phase)s")
        params["phase"] = phase
    params["limit"] = max(1, min(int(limit), MAX_ROWS))
    rows = db.fetch_all(f"""
        select a.id, a.match_id, m.played_on, h.name as home, v.name as visiting, a.set_number,
               a.home_score_start as home_score, a.visiting_score_start as visiting_score,
               t.name as team, a.shirt_number, p.full_name as player,
               a.skill, a.skill_type, a.evaluation_code, a.evaluation, a.attack_code, a.set_code,
               a.start_zone, a.end_zone, a.phase, a.video_time_s, a.code,
               exists (select 1 from video where match_id = a.match_id) as has_video
        from action a
        join match m on m.id = a.match_id
        join team h on h.id = m.home_team_id
        join team v on v.id = m.visiting_team_id
        join team t on t.id = a.team_id
        left join person p on p.id = a.person_id
        where {' and '.join(where)}
        order by m.played_on, a.seq
        limit %(limit)s""", params)
    for r in rows:
        if r["has_video"] and r["video_time_s"] is not None:
            t = float(r["video_time_s"])
            r["watch_url"] = watch_url(r["match_id"], max(0, t - 4), t + 4)
    return _plain(rows)


def stats(user: User, *, skill: str, group_by: str = "player", player: str | None = None,
          team: str | None = None, opponent: str | None = None, match_id: int | None = None,
          skill_type: str | None = None, attack_code: str | None = None) -> list[dict]:
    """Counts per evaluation code plus the usual percentages for one skill."""
    groups = {
        "player": [("p.full_name", "player"), ("t.name", "team")],
        "team": [("t.name", "team")],
        "match": [("m.played_on", "played_on"), ("h.name", "home"), ("v.name", "visiting")],
        "skill_type": [("a.skill_type", "skill_type")],
        "attack_code": [("a.attack_code", "attack_code")],
        "set_number": [("a.set_number", "set_number")],
    }
    if group_by not in groups:
        raise ValueError(f"group_by must be one of {', '.join(groups)}")
    canonical = next((s for s in SKILLS if s.lower() == skill.lower()), None)
    if canonical is None:
        raise ValueError(f"skill must be one of {', '.join(SKILLS)}")
    vis, params = _visible_matches_clause(user)
    where = [vis, "a.skill = %(skill)s"]
    params["skill"] = canonical
    for key, clause, value in (
        ("player", "p.full_name ilike %(player)s", f"%{player}%" if player else None),
        ("team", "t.name ilike %(team)s", f"%{team}%" if team else None),
        ("match_id", "a.match_id = %(match_id)s", match_id),
        ("skill_type", "a.skill_type ilike %(skill_type)s", f"%{skill_type}%" if skill_type else None),
        ("attack_code", "a.attack_code = %(attack_code)s", attack_code.upper() if attack_code else None),
    ):
        if value:
            where.append(clause)
            params[key] = value
    if opponent:
        where.append("""exists (select 1 from team o where o.id in (m.home_team_id, m.visiting_team_id)
                        and o.id <> a.team_id and o.name ilike %(opponent)s)""")
        params["opponent"] = f"%{opponent}%"
    cols = groups[group_by]
    select_cols = ", ".join(f"{expr} as {name}" for expr, name in cols)
    group_cols = ", ".join(expr for expr, _ in cols)
    result = db.fetch_all(f"""
        select {select_cols},
               count(*) as total,
               count(*) filter (where a.evaluation_code = '#') as "#",
               count(*) filter (where a.evaluation_code = '+') as "+",
               count(*) filter (where a.evaluation_code = '!') as "!",
               count(*) filter (where a.evaluation_code = '-') as "-",
               count(*) filter (where a.evaluation_code = '/') as "/",
               count(*) filter (where a.evaluation_code = '=') as "=",
               round(100.0 * count(*) filter (where a.evaluation_code = '#') / count(*), 1) as pct_perfect_or_point,
               round(100.0 * count(*) filter (where a.evaluation_code in ('#', '+')) / count(*), 1) as pct_positive,
               round(100.0 * count(*) filter (where a.evaluation_code = '=') / count(*), 1) as pct_error,
               round(100.0 * (count(*) filter (where a.evaluation_code = '#')
                              - count(*) filter (where a.evaluation_code in ('=', '/'))) / count(*), 1) as efficiency
        from action a
        join match m on m.id = a.match_id
        join team h on h.id = m.home_team_id
        join team v on v.id = m.visiting_team_id
        join team t on t.id = a.team_id
        left join person p on p.id = a.person_id
        where {' and '.join(where)}
        group by {group_cols}
        order by total desc""", params)
    return _plain(result)


# ------------------------------------------------------------------ clips

def _clip_public(row: dict) -> dict:
    out = {k: row[k] for k in ("id", "title", "caption", "status", "duration_s", "created_by", "created_at", "error")
           if k in row}
    if row.get("status") == "ready":
        out["url"] = auth.signed_url(f"/media/clips/{row['id']}.mp4")
    return _plain([out])[0]


def _default_caption(a: dict) -> str:
    who = f"#{a['shirt_number']} {a['player']}" if a.get("player") else a["team"]
    what = " ".join(x for x in (a["skill"], a.get("evaluation") or a.get("evaluation_code") or "") if x)
    return (f"Set {a['set_number']}   {a['home']} {a['home_score']}-{a['visiting_score']} {a['visiting']}"
            f"   {who} - {what}")


def create_clip(user: User, *, action_ids: list[int] | None = None, tag_ids: list[int] | None = None,
                caption: str | None = None, title: str | None = None, before_s: float = 4.0,
                after_s: float = 4.0, slow_motion: bool = True) -> dict:
    """Queue a clip made of one segment per action/tag. The same request returns the same clip."""
    action_ids = list(dict.fromkeys(action_ids or []))
    tag_ids = list(dict.fromkeys(tag_ids or []))
    if not action_ids and not tag_ids:
        raise ValueError("give action_ids and/or tag_ids")
    if len(action_ids) + len(tag_ids) > 60:
        raise ValueError("at most 60 segments per clip")
    before_s = max(0.0, min(float(before_s), 30.0))
    after_s = max(0.0, min(float(after_s), 30.0))
    vis, params = _visible_matches_clause(user)

    segments: list[dict] = []
    if action_ids:
        params["ids"] = action_ids
        rows = db.fetch_all(f"""
            select a.id, a.match_id, a.set_number, a.home_score_start as home_score,
                   a.visiting_score_start as visiting_score, h.name as home, v.name as visiting,
                   t.name as team, a.shirt_number, p.full_name as player, a.skill, a.evaluation,
                   a.evaluation_code, a.video_time_s, vid.storage_key
            from action a
            join match m on m.id = a.match_id
            join team h on h.id = m.home_team_id join team v on v.id = m.visiting_team_id
            join team t on t.id = a.team_id
            left join person p on p.id = a.person_id
            left join video vid on vid.match_id = a.match_id
            where a.id = any(%(ids)s) and {vis}""", params)
        found = {r["id"]: r for r in rows}
        missing = [i for i in action_ids if i not in found]
        if missing:
            raise NotFound(f"actions not found or not visible: {missing}")
        for i in action_ids:
            r = found[i]
            if not r["storage_key"] or r["video_time_s"] is None:
                raise ValueError(f"action {i} has no video")
            t = float(r["video_time_s"])
            segments.append({"match_id": r["match_id"], "video": r["storage_key"],
                             "start": round(max(0.0, t - before_s), 2), "end": round(t + after_s, 2),
                             "slowmo": [round(max(0.0, t - 1), 2), round(t + 1, 2)] if slow_motion else None,
                             "caption": caption or _default_caption(r)})
    if tag_ids:
        params["tag_ids"] = tag_ids
        rows = db.fetch_all(f"""
            select g.id, g.match_id, g.label, g.set_number, g.video_time_s, vid.storage_key
            from tag g join match m on m.id = g.match_id left join video vid on vid.match_id = g.match_id
            where g.id = any(%(tag_ids)s) and {vis}""", params)
        found = {r["id"]: r for r in rows}
        missing = [i for i in tag_ids if i not in found]
        if missing:
            raise NotFound(f"tags not found or not visible: {missing}")
        for i in tag_ids:
            r = found[i]
            if not r["storage_key"] or r["video_time_s"] is None:
                raise ValueError(f"tag {i} has no video")
            t = float(r["video_time_s"])
            segments.append({"match_id": r["match_id"], "video": r["storage_key"],
                             "start": round(max(0.0, t - before_s), 2), "end": round(t + after_s, 2),
                             "slowmo": [round(max(0.0, t - 1), 2), round(t + 1, 2)] if slow_motion else None,
                             "caption": caption or f"Set {r['set_number']}   {r['label']}"})

    spec = {"version": 1, "segments": segments}
    params_hash = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    with db.conn() as c:
        row = c.execute("select * from clip where params_hash = %s", (params_hash,)).fetchone()
        if row and row["status"] == "failed":
            row = c.execute("update clip set status = 'queued', error = null where id = %s returning *",
                            (row["id"],)).fetchone()
        if row is None:
            row = c.execute("""
                insert into clip (params_hash, title, caption, spec, created_by)
                values (%s, %s, %s, %s, %s) returning *""",
                (params_hash, title, caption, json.dumps(spec), user.name)).fetchone()
            if action_ids:
                c.execute("insert into clip_action (clip_id, action_id) select %s, unnest(%s::bigint[])",
                          (row["id"], action_ids))
    return _clip_public(row)


def _clip_visible(user: User, clip_id: int) -> dict:
    row = db.fetch_one("select * from clip where id = %s", (clip_id,))
    if row is None:
        raise NotFound(f"clip {clip_id} not found")
    if not user.sees_all_teams:
        match_ids = {s["match_id"] for s in row["spec"]["segments"]}
        vis, params = _visible_matches_clause(user)
        params["ids"] = list(match_ids)
        ok = db.fetch_one(f"select count(*) as n from match m where m.id = any(%(ids)s) and {vis}", params)
        if ok["n"] != len(match_ids):
            raise NotFound(f"clip {clip_id} not found")
    return row


def get_clip(user: User, clip_id: int) -> dict:
    return _clip_public(_clip_visible(user, clip_id))


def list_clips(user: User, mine: bool = False, limit: int = 50) -> list[dict]:
    rows = db.fetch_all("select * from clip order by id desc limit %s", (max(1, min(limit, MAX_ROWS)) * 4,))
    out = []
    for r in rows:
        if mine and r["created_by"] != user.name:
            continue
        try:
            _clip_visible(user, r["id"])
        except NotFound:
            continue
        out.append(_clip_public(r))
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ staff

def run_query(user: User, sql: str, limit: int = 200) -> list[dict]:
    """Read-only SQL for staff, executed as a role that cannot see users or staff notes."""
    if not user.is_staff:
        raise Forbidden("only staff can run SQL")
    with db.conn() as c:
        with c.transaction():
            c.execute("set transaction read only")
            c.execute("set local role volleyhub_reader")
            c.execute("set local statement_timeout = '10s'")
            cur = c.execute(pgsql.SQL(sql))  # noqa: S608 - read-only role, read-only transaction
            rows = cur.fetchmany(max(1, min(limit, 2000))) if cur.description else []
    return _plain(rows)


def player_notes(user: User, player: str) -> list[dict]:
    if not user.is_staff:
        raise Forbidden("staff only")
    return _plain(db.fetch_all("""
        select n.id, p.full_name, t.name as team, n.noted_on, n.body, n.created_by
        from staff_note n join person p on p.id = n.person_id left join team t on t.id = n.team_id
        where p.full_name ilike %s order by n.noted_on desc""", (f"%{player}%",)))
