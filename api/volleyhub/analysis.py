"""Analyses on top of the action/rally tables: scout checks, rally phases, opponent report.

Definitions follow common volleyball analytics (DataVolley conventions):
- side-out: rallies where the team receives; break point: rallies where it serves.
- K1: the team's attack in the reception phase (first attack after its reception).
- FBSO (first-ball side-out): receiving rallies won directly by that first attack.
- transition: attacks in the transition phase (after a dig or a freeball), both when serving
  (counter-attack, break point) and when receiving after the first attack.
"""

from . import db
from .auth import User
from .core import Forbidden, NotFound, _plain, _visible_matches_clause

# Serve evaluation -> reception evaluations that agree with it (DataVolley pairing).
SERVE_RECEPTION_PAIRS = {"#": ["="], "/": ["/"], "+": ["-"], "!": ["!"], "-": ["+", "#"]}


def _team(name: str | None, *, default_ours: bool = False) -> dict:
    if not name:
        if default_ours:
            ours = db.fetch_all("select id, name from team where is_ours order by id")
            if len(ours) == 1:
                return ours[0]
            raise ValueError("say which team: " + ", ".join(t["name"] for t in ours))
        raise ValueError("team name required")
    rows = db.fetch_all("""select id, name from team
                           where name ilike %(like)s or lower(%(name)s) = any (select lower(n) from unnest(dv_names) n)
                           order by name""", {"like": f"%{name}%", "name": name})
    if not rows:
        raise NotFound(f"no team matches '{name}'")
    exact = [r for r in rows if r["name"].lower() == name.lower()]
    if len(rows) > 1 and not exact:
        raise ValueError(f"'{name}' matches several teams: " + ", ".join(r["name"] for r in rows))
    return (exact or rows)[0]


# ------------------------------------------------------------------ 1. scout check

def scout_check(user: User, *, match_id: int | None = None) -> list[dict]:
    """Inconsistencies a scout should look at, one finding per rally (or per match/player for summaries)."""
    if not user.sees_all_teams and not user.is_staff:
        raise Forbidden("scout checks are for staff and management")
    vis, params = _visible_matches_clause(user)
    where = [vis]
    if match_id:
        where.append("m.id = %(match_id)s")
        params["match_id"] = match_id
    w = " and ".join(where)
    params["pairs"] = [f"{s}{r}" for s, rs in SERVE_RECEPTION_PAIRS.items() for r in rs]

    base = f"""
        with acts as (
            select a.*, m.played_on, m.home_team_id, m.visiting_team_id, r.won_by_team_id,
                   r.home_score_start as rh, r.visiting_score_start as rv,
                   lead(a.skill) over w as next_skill, lead(a.evaluation_code) over w as next_eval,
                   lead(a.team_id) over w as next_team,
                   lag(a.video_time_s) over w as prev_time,
                   row_number() over (partition by a.match_id, a.point_id order by a.seq desc) as from_end,
                   count(*) over (partition by a.match_id, a.point_id) as n_in_rally
            from action a
            join match m on m.id = a.match_id
            join rally r on r.match_id = a.match_id and r.point_id = a.point_id
            where {w}
            window w as (partition by a.match_id, a.point_id order by a.seq)
        ),
        rally_codes as (
            select match_id, point_id, string_agg(code, ' ' order by seq) as codes, array_agg(id order by seq) as ids
            from acts group by 1, 2
        )"""

    def rally_findings(rule: str, message_sql: str, cond: str) -> list[dict]:
        return db.fetch_all(base + f"""
            select distinct on (x.match_id, x.point_id) %(rule)s as rule, x.match_id, x.played_on, x.set_number,
                   x.rh || '-' || x.rv as score, x.point_id, {message_sql} as message, rc.codes, rc.ids as action_ids
            from acts x join rally_codes rc using (match_id, point_id)
            where {cond}
            order by x.match_id, x.point_id, x.seq""", {**params, "rule": rule})

    out: list[dict] = []
    out += rally_findings(
        "serve_reception_mismatch",
        "'serve ' || x.evaluation_code || ' with reception ' || x.next_eval || ' (they should agree)'",
        "x.skill = 'Serve' and x.next_skill = 'Reception' and x.evaluation_code || x.next_eval <> all(%(pairs)s)")
    out += rally_findings(
        "serve_outcome_but_rally_continues",
        "'serve ' || x.evaluation_code || ' but the rally has ' || x.n_in_rally || ' touches'",
        "x.skill = 'Serve' and ((x.evaluation_code = '#' and x.n_in_rally > 2) or (x.evaluation_code = '=' and x.n_in_rally > 1))")
    out += rally_findings(
        "video_time_goes_back",
        "'video time goes back ' || round(x.prev_time - x.video_time_s) || ' s at ' || x.code",
        "x.prev_time is not null and x.video_time_s < x.prev_time - 1")
    out += rally_findings(
        "video_time_gap",
        "'gap of ' || round(x.video_time_s - x.prev_time) || ' s before ' || x.code",
        "x.prev_time is not null and x.video_time_s > x.prev_time + 20")
    out += rally_findings(
        "winner_vs_last_touch",
        "'last touch ' || x.code || ' but the point goes to the ' || "
        "case when x.won_by_team_id = x.home_team_id then 'home' else 'visiting' end || ' team'",
        "x.from_end = 1 and x.won_by_team_id is not null and ("
        "(x.evaluation_code = '=' and x.team_id = x.won_by_team_id) or "
        "(x.evaluation_code = '#' and x.skill in ('Serve', 'Attack', 'Block') and x.team_id <> x.won_by_team_id))")
    out += rally_findings(
        "kill_then_play_continues",
        "'kill ' || x.code || ' but the rally goes on with ' || x.next_skill || ' ' || x.next_eval",
        "x.skill = 'Attack' and x.evaluation_code = '#' and x.next_skill is not null and ("
        "x.next_team = x.team_id or x.next_skill not in ('Block', 'Dig') or "
        "(x.from_end = 2 and x.next_eval <> '='))")

    # Per match / per player summaries.
    out += db.fetch_all(f"""
        with s as (
            select a.match_id, a.person_id, a.shirt_number, a.skill_type, count(*) as n
            from action a join team tm on tm.id = a.team_id and tm.is_ours
            where a.skill = 'Serve' group by 1, 2, 3, 4),
        flagged as (  -- more than one type, and some type outside the ones declared for the player
            select s.match_id, s.person_id, s.shirt_number from s left join person p on p.id = s.person_id
            group by 1, 2, 3, p.serve_types
            having count(*) > 1 and (p.serve_types is null or bool_or(not (s.skill_type = any(p.serve_types)))))
        select 'serve_types_mixed' as rule, m.id as match_id, m.played_on, null::int as set_number, null as score,
               null::int as point_id,
               coalesce(p.full_name, '#' || s.shirt_number) || ' serves ' ||
                 string_agg(s.skill_type || ' ' || s.n, ', ' order by s.n desc) ||
                 ' (a typo? if the player really uses these, set person.serve_types)' as message,
               null as codes, null::bigint[] as action_ids
        from s join flagged f using (match_id, person_id, shirt_number)
        join match m on m.id = s.match_id left join person p on p.id = s.person_id
        where {w}
        group by m.id, m.played_on, p.full_name, s.shirt_number""", params)
    out += db.fetch_all(f"""
        select 'zones_read_as_cones' as rule, m.id as match_id, m.played_on, null::int as set_number, null as score,
               null::int as point_id,
               count(*) || ' attacks have a cone and a subzone: subzones go with zones, so the .dvw header probably '
                 || 'says cones (C) while the scout used zones (Z)' as message,
               null as codes, null::bigint[] as action_ids
        from action a join match m on m.id = a.match_id
        where {w} and a.skill = 'Attack' and a.end_cone is not null and a.end_subzone is not null
        group by m.id, m.played_on having count(*) > 0""", params)
    out += db.fetch_all(f"""
        with c as (
            select m.id, m.played_on, r.set_number, m.set_scores[r.set_number] as written,
                   count(*) filter (where r.won_by_team_id = m.home_team_id) || '-' ||
                   count(*) filter (where r.won_by_team_id = m.visiting_team_id) as counted
            from rally r join match m on m.id = r.match_id where {w}
            group by m.id, m.played_on, r.set_number, m.set_scores)
        select 'set_score_mismatch' as rule, id as match_id, played_on, set_number, null as score, null::int as point_id,
               'rally winners give ' || counted || ', the file says ' || written || ' (a point given to the wrong team?)'
                 as message, null as codes, null::bigint[] as action_ids
        from c where written is not null and counted <> written""", params)
    order = {"set_score_mismatch": 0, "zones_read_as_cones": 1, "winner_vs_last_touch": 2,
             "serve_reception_mismatch": 3, "serve_outcome_but_rally_continues": 4, "kill_then_play_continues": 5,
             "video_time_goes_back": 6, "video_time_gap": 7, "serve_types_mixed": 8}
    out.sort(key=lambda f: (f["match_id"], order.get(f["rule"], 9), f["set_number"] or 0, f["point_id"] or 0))
    return _plain(out)


# ------------------------------------------------------------------ 2. rally phases

PHASE_GROUPS = {
    "none": ("'all'", "1"),
    "match": ("played_on::text || ' vs ' || opp", "played_on"),
    "set": ("'set ' || set_number", "set_number"),
    "rotation": ("'P' || rotation", "rotation"),
}


def _pct(num: str, den: str) -> str:
    return f"round(100.0 * ({num}) / nullif({den}, 0), 1)"


def phases(user: User, *, team: str | None = None, opponent: str | None = None, match_id: int | None = None,
           group_by: str = "none") -> list[dict]:
    """Side-out, break point, K1, FBSO, transition and freeball numbers for one team."""
    if group_by not in PHASE_GROUPS:
        raise ValueError(f"group_by must be one of {', '.join(PHASE_GROUPS)}")
    t = _team(team, default_ours=True)
    vis, params = _visible_matches_clause(user)
    params["t"] = t["id"]
    where = [vis, "%(t)s in (m.home_team_id, m.visiting_team_id)"]
    if match_id:
        where.append("m.id = %(match_id)s")
        params["match_id"] = match_id
    if opponent:
        where.append("o.name ilike %(opp)s")
        params["opp"] = f"%{opponent}%"
    label, order = PHASE_GROUPS[group_by]
    own = "a.match_id = r.match_id and a.point_id = r.point_id and a.team_id = %(t)s"
    rows = db.fetch_all(f"""
        with x as (
            select m.played_on, o.name as opp, r.set_number,
                   r.serving_team_id <> %(t)s as receiving, r.won_by_team_id = %(t)s as won,
                   case when m.home_team_id = %(t)s then r.home_setter_position else r.visiting_setter_position end
                     as rotation,
                   (select a.evaluation_code from action a where {own} and a.skill = 'Attack' and a.phase = 'Reception'
                    order by a.seq limit 1) as k1,
                   (select count(*) from action a where {own} and a.skill = 'Attack' and a.phase = 'Transition') as tr_n,
                   (select count(*) from action a where {own} and a.skill = 'Attack' and a.phase = 'Transition'
                      and a.evaluation_code = '#') as tr_kill,
                   (select count(*) from action a where {own} and a.skill = 'Attack' and a.phase = 'Transition'
                      and a.evaluation_code in ('=', '/')) as tr_err,
                   exists (select 1 from action a where a.match_id = r.match_id and a.point_id = r.point_id
                           and a.skill = 'Freeball') as freeball
            from rally r join match m on m.id = r.match_id
            join team o on o.id = case when m.home_team_id = %(t)s then m.visiting_team_id else m.home_team_id end
            where {' and '.join(where)})
        select {label} as grp,
               count(*) filter (where receiving) as sideout_rallies,
               {_pct("count(*) filter (where receiving and won)", "count(*) filter (where receiving)")} as sideout_pct,
               count(*) filter (where not receiving) as breakpoint_rallies,
               {_pct("count(*) filter (where not receiving and won)", "count(*) filter (where not receiving)")} as breakpoint_pct,
               count(*) filter (where receiving and k1 is not null) as k1_attacks,
               {_pct("count(*) filter (where k1 = '#')", "count(*) filter (where receiving and k1 is not null)")} as k1_kill_pct,
               {_pct("count(*) filter (where k1 in ('=', '/'))", "count(*) filter (where receiving and k1 is not null)")} as k1_error_pct,
               {_pct("count(*) filter (where k1 = '#') - count(*) filter (where k1 in ('=', '/'))", "count(*) filter (where receiving and k1 is not null)")} as k1_efficiency,
               {_pct("count(*) filter (where receiving and k1 = '#' and won)", "count(*) filter (where receiving)")} as fbso_pct,
               sum(tr_n) filter (where not receiving) as counterattacks,
               {_pct("sum(tr_kill) filter (where not receiving)", "sum(tr_n) filter (where not receiving)")} as counterattack_kill_pct,
               {_pct("sum(tr_kill) filter (where not receiving) - sum(tr_err) filter (where not receiving)", "sum(tr_n) filter (where not receiving)")} as counterattack_efficiency,
               sum(tr_n) filter (where receiving) as sideout_transition_attacks,
               {_pct("sum(tr_kill) filter (where receiving) - sum(tr_err) filter (where receiving)", "sum(tr_n) filter (where receiving)")} as sideout_transition_efficiency,
               count(*) filter (where freeball) as freeball_rallies,
               {_pct("count(*) filter (where freeball and won)", "count(*) filter (where freeball)")} as freeball_won_pct
        from x group by grp, {order} order by {order}""", params)
    for r in rows:
        r["team"] = t["name"]
    return _plain(rows)


# ------------------------------------------------------------------ 5. opponent report

def opponent_report(user: User, *, opponent: str, last_matches: int | None = None) -> dict:
    """Tables to prepare a match: how they serve, receive, distribute, attack and block (from our scouts)."""
    o = _team(opponent)
    vis, params = _visible_matches_clause(user)
    params["o"] = o["id"]
    mrows = db.fetch_all(f"""select m.id, m.played_on, h.name as home, v.name as visiting from match m
                             join team h on h.id = m.home_team_id join team v on v.id = m.visiting_team_id
                             where {vis} and %(o)s in (m.home_team_id, m.visiting_team_id)
                             order by m.played_on desc""", params)
    if last_matches:
        mrows = mrows[:max(1, int(last_matches))]
    if not mrows:
        raise NotFound(f"no visible matches with {o['name']}")
    params["mids"] = [r["id"] for r in mrows]
    # a: actions with player name and the opponent's setter rotation in that rally
    a_cte = """
        with a as (
            select a.*, coalesce(p.full_name, '#' || a.shirt_number) as player,
                   case when m.home_team_id = %(o)s then r.home_setter_position else r.visiting_setter_position end
                     as o_rotation
            from action a join match m on m.id = a.match_id
            join rally r on r.match_id = a.match_id and r.point_id = a.point_id
            left join person p on p.id = a.person_id
            where a.match_id = any(%(mids)s))"""
    eff = "round(100.0 * (count(*) filter (where evaluation_code = '#') - count(*) filter (where evaluation_code in ('=', '/'))) / count(*), 1)"
    pos = "round(100.0 * count(*) filter (where evaluation_code in ('#', '+')) / count(*), 1)"

    def q(sql: str) -> list[dict]:
        return _plain(db.fetch_all(a_cte + sql, params))

    report = {
        "opponent": o["name"],
        "matches": _plain(mrows),
        "how_they_serve": q(f"""
            select player, count(*) as serves,
                   string_agg(distinct skill_type, ', ') as types,
                   round(100.0 * count(*) filter (where evaluation_code = '#') / count(*), 1) as ace_pct,
                   round(100.0 * count(*) filter (where evaluation_code = '=') / count(*), 1) as error_pct,
                   {eff} as efficiency,
                   (select string_agg(z, ' ') from (select 'z' || end_zone || ':' || count(*) as z from a s
                     where s.team_id = %(o)s and s.skill = 'Serve' and s.player = a.player and s.end_zone is not null
                     group by end_zone order by count(*) desc limit 4) t) as main_targets,
                   (select string_agg(z, ' ') from (select 'z' || start_zone || ':' || count(*) as z from a s
                     where s.team_id = %(o)s and s.skill = 'Serve' and s.player = a.player and s.start_zone is not null
                     group by start_zone order by count(*) desc limit 3) t) as start_zones
            from a where team_id = %(o)s and skill = 'Serve' group by player order by count(*) desc"""),
        "their_reception_by_rotation": q(f"""
            select 'P' || o_rotation as rotation, count(*) as receptions, {pos} as positive_pct,
                   round(100.0 * count(*) filter (where evaluation_code = '=') / count(*), 1) as error_pct,
                   (select string_agg(x, ', ') from (select b.player || ' ' || count(*) || ' (' ||
                       round(100.0 * count(*) filter (where b.evaluation_code in ('#', '+')) / count(*)) || '%% pos)' as x
                     from a b where b.team_id = %(o)s and b.skill = 'Reception' and b.o_rotation = a.o_rotation
                     group by b.player order by count(*) desc limit 3) t) as main_receivers
            from a where team_id = %(o)s and skill = 'Reception' group by o_rotation order by o_rotation"""),
        "their_reception_by_target_zone": q(f"""
            select end_zone as served_to_zone, count(*) as receptions, {pos} as positive_pct,
                   round(100.0 * count(*) filter (where evaluation_code in ('=', '/')) / count(*), 1) as error_or_overpass_pct
            from a where team_id = %(o)s and skill = 'Reception' and end_zone is not null
            group by end_zone order by positive_pct, receptions desc"""),
        "setting_distribution": q("""
            , k1 as (  -- their first attack after their own reception, with how good that reception was
                select distinct on (x.match_id, x.point_id) x.*, rc.evaluation_code in ('#', '+') as good
                from a x join a rc on rc.match_id = x.match_id and rc.point_id = x.point_id
                     and rc.team_id = %(o)s and rc.skill = 'Reception'
                where x.team_id = %(o)s and x.skill = 'Attack' and x.phase = 'Reception'
                order by x.match_id, x.point_id, x.seq)
            select 'P' || o_rotation as rotation,
                   case when good then 'good reception (#/+)' else 'poor reception' end as reception,
                   count(*) as first_attacks,
                   string_agg(distinct attack_code, ' ') as attack_codes,
                   (select string_agg(z, ' ') from (select 'from ' || k.start_zone || ': ' ||
                       round(100.0 * count(*) / sum(count(*)) over ()) || '%%' as z from k1 k
                     where k.o_rotation = k1.o_rotation and k.good = k1.good
                     group by k.start_zone order by count(*) desc) t) as distribution,
                   round(100.0 * count(*) filter (where evaluation_code = '#') / count(*), 1) as kill_pct
            from k1 group by o_rotation, good order by o_rotation, good desc"""),
        "attackers": q(f"""
            select player, count(*) as attacks,
                   round(100.0 * count(*) filter (where evaluation_code = '#') / count(*), 1) as kill_pct,
                   round(100.0 * count(*) filter (where evaluation_code = '=') / count(*), 1) as error_pct,
                   round(100.0 * count(*) filter (where evaluation_code = '/') / count(*), 1) as blocked_pct,
                   {eff} as efficiency,
                   (select string_agg(z, ' ') from (select 'from ' || s.start_zone || ':' || count(*) as z from a s
                     where s.team_id = %(o)s and s.skill = 'Attack' and s.player = a.player and s.start_zone is not null
                     group by s.start_zone order by count(*) desc limit 3) t) as start_zones,
                   (select string_agg(z, ' ') from (select 'to ' || s.end_zone || ':' || count(*) as z from a s
                     where s.team_id = %(o)s and s.skill = 'Attack' and s.player = a.player and s.end_zone is not null
                     group by s.end_zone order by count(*) desc limit 4) t) as end_zones
            from a where team_id = %(o)s and skill = 'Attack' group by player order by count(*) desc"""),
        "block_and_defence": q("""
            select player,
                   count(*) filter (where skill = 'Block') as blocks,
                   count(*) filter (where skill = 'Block' and evaluation_code = '#') as block_points,
                   count(*) filter (where skill = 'Dig') as digs,
                   round(100.0 * count(*) filter (where skill = 'Dig' and evaluation_code in ('#', '+'))
                         / nullif(count(*) filter (where skill = 'Dig'), 0), 1) as dig_positive_pct
            from a where team_id = %(o)s and skill in ('Block', 'Dig') group by player
            order by block_points desc, blocks desc"""),
    }
    return report
