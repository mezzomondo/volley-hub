"""HTTP entry point: MCP server at /mcp, REST API (schema at /openapi.json), signed media files. No web pages."""

import contextvars
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import anyio
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from urllib.parse import urlparse

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

import time

from . import analysis, auth, config, core, db, drawing, library, log, migrate, oauth, reports, usage, usage
from .auth import User

# ------------------------------------------------------------------ auth plumbing

_current_user: contextvars.ContextVar[User | None] = contextvars.ContextVar("current_user", default=None)
PUBLIC_PREFIXES = ("/healthz", "/media/", "/openapi.json") + oauth.PUBLIC_PATHS


def _user_for_bearer(token: str | None) -> User | None:
    """Personal access codes (Claude Code, scripts) or OAuth access tokens (connectors)."""
    if not token:
        return None
    return auth.user_for_token(token) or oauth.user_for_access_token(token)


_WWW_AUTH = f'Bearer realm="volley-hub", resource_metadata="{oauth.RESOURCE_METADATA_URL}"'.encode()


class AuthMiddleware:
    """Bearer-token check for everything except public and signed paths (pure ASGI, keeps streaming)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        started, status, user = time.monotonic(), [0], None

        async def send_logged(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)

        try:
            if path == "/" or path.startswith(PUBLIC_PREFIXES):
                return await self.app(scope, receive, send_logged)
            user = await anyio.to_thread.run_sync(_user_for_bearer, auth.bearer(headers))
            if user is None:
                body = b'{"detail":"missing or invalid bearer token"}'
                await send_logged({"type": "http.response.start", "status": 401,
                                   "headers": [(b"content-type", b"application/json"),
                                               (b"www-authenticate", _WWW_AUTH)]})
                await send({"type": "http.response.body", "body": body})
                return
            scope.setdefault("state", {})["user"] = user
            token = _current_user.set(user)
            try:
                await self.app(scope, receive, send_logged)
            finally:
                _current_user.reset(token)
        finally:
            if path != "/healthz":  # path only: signed links carry their signature in the query string
                log.event("http", method=scope.get("method"), path=path, status=status[0] or 500,
                          ms=round((time.monotonic() - started) * 1000),
                          user=user.name if user else None, role=user.role if user else None,
                          ip=(headers.get("x-forwarded-for") or "").split(",")[0].strip() or None,
                          agent=(headers.get("user-agent") or "")[:120] or None)


def current_user() -> User:
    user = _current_user.get()
    if user is None:
        raise PermissionError("not authenticated")
    return user


def rest_user(request: Request) -> User:
    user = request.scope.get("state", {}).get("user")
    if user is None:
        raise HTTPException(401, "missing or invalid bearer token")
    return user


async def _run(fn, *args, **kwargs):
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
    except core.NotFound as e:
        raise HTTPException(404, str(e))
    except core.Forbidden as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ MCP

mcp = MCPServer(
    "volley-hub",
    instructions=(
        "Volleyball match data (DataVolley scouting) and video for a club. "
        "Typical flow: list_matches / list_players to find names and ids, search_actions to find touches, "
        "stats for aggregates, create_clip to turn actions into a video clip (poll get_clip until status is "
        "'ready', then share its url). Evaluation codes: # = perfect/point, + = positive, ! = ok/exclamation, "
        "- = poor, / = blocked or overpass (attack: blocked), = = error. Skills: Serve, Reception, Set, "
        "Attack, Block, Dig, Freeball. Attack codes are DataVolley combination codes (e.g. X5 = high ball "
        "position 4, V5 = high ball, X1 = quick, XP/VP = pipe). Scores are at the start of the rally."
    ),
)


async def _tool(fn, **kwargs):
    user = current_user()
    started, error = time.monotonic(), None
    try:
        return await anyio.to_thread.run_sync(lambda: fn(user, **kwargs))
    except Exception as e:
        error = " ".join(f"{type(e).__name__}: {e}".split())
        raise
    finally:
        log.event("tool", tool={"run_query": "query"}.get(fn.__name__, fn.__name__), user=user.name, role=user.role,
                  args={k: log.short(v) for k, v in kwargs.items() if v not in (None, [], "")},
                  ms=round((time.monotonic() - started) * 1000), ok=error is None, error=log.short(error, 300) if error else None)


@mcp.tool()
async def list_matches(team: str | None = None) -> list[dict]:
    """Matches you can see, oldest first. Optional team name filter (partial match)."""
    return await _tool(core.list_matches, team=team)


@mcp.tool()
async def list_players(team: str | None = None) -> list[dict]:
    """Players who appeared in visible matches, with team, role and shirt numbers used.
    Shirt numbers can change between matches: always refer to players by name."""
    return await _tool(core.list_players, team=team)


@mcp.tool()
async def list_tags(match_id: int | None = None) -> list[dict]:
    """Labels written by the scout during matches (e.g. 'block touch'), with video links."""
    return await _tool(core.list_tags, match_id=match_id)


@mcp.tool()
async def search_actions(player: str | None = None, team: str | None = None, opponent: str | None = None,
                         match_id: int | None = None, skill: str | None = None,
                         evaluation: list[str] | None = None, skill_type: str | None = None,
                         attack_code: str | None = None, set_number: int | None = None,
                         phase: str | None = None, start_zone: int | None = None, end_zone: int | None = None,
                         end_cone: int | None = None, end_subzone: str | None = None,
                         prev_skill: str | None = None, prev_evaluation: list[str] | None = None,
                         prev_skill_type: str | None = None, serve_type: str | None = None,
                         reception_evaluation: list[str] | None = None, setter_position: int | None = None,
                         late_set: bool | None = None, limit: int = 100) -> list[dict]:
    """Find touches. All filters optional and combined with AND. player/team/opponent are partial names.
    evaluation is a list of codes, e.g. ['=', '/'] for errors and blocked attacks.
    phase: Serve, Reception (side-out) or Transition. Results with video have a video_url: the match video starting 4 s before the touch (signed link, a file, no page). To watch or share a touch, prefer create_clip.
    Zones and direction (court zones 1-9): start_zone is where the touch starts (attack: 4 = left, 3 = middle,
    2 = right, 9/8/7 = back row), end_zone where the ball lands, end_subzone A-D inside that zone. Some scouts
    record attack direction as end_cone (DataVolley cones, counted from the start zone) instead of end_zone; for
    serves and attacks end_zone is then computed from the end coordinates (end_zone_source 'coordinates' instead
    of 'code': less precise). start_x/start_y/end_x/end_y are court coordinates (x 0.5-3.5 across, y 0.5-6.5
    along the court, net at y 3.5), for heatmaps.
    Rally context: prev_skill (+ prev_evaluation, prev_skill_type) keeps touches that come after such a touch of
    the same team in the same possession, e.g. skill='Attack', prev_skill='Reception', prev_evaluation=['#'] =
    attacks after a perfect pass; prev_skill='Dig' = attacks after a dig. serve_type filters on the rally's serve
    (e.g. 'jump-float'), reception_evaluation on the rally's reception. setter_position = rotation (setter in
    P1-P6) of the touch's team. late_set = from 20 (12 in the fifth set)."""
    return await _tool(core.search_actions, player=player, team=team, opponent=opponent, match_id=match_id,
                       skill=skill, evaluation=evaluation, skill_type=skill_type, attack_code=attack_code,
                       set_number=set_number, phase=phase, start_zone=start_zone, end_zone=end_zone,
                       end_cone=end_cone, end_subzone=end_subzone, prev_skill=prev_skill, prev_evaluation=prev_evaluation,
                       prev_skill_type=prev_skill_type, serve_type=serve_type,
                       reception_evaluation=reception_evaluation, setter_position=setter_position, late_set=late_set, limit=limit)


@mcp.tool()
async def stats(skill: str, group_by: str = "player", player: str | None = None, team: str | None = None,
                opponent: str | None = None, match_id: int | None = None, skill_type: str | None = None,
                attack_code: str | None = None, start_zone: int | None = None, end_zone: int | None = None,
                end_cone: int | None = None, end_subzone: str | None = None, phase: str | None = None,
                prev_skill: str | None = None, prev_evaluation: list[str] | None = None,
                prev_skill_type: str | None = None, serve_type: str | None = None,
                reception_evaluation: list[str] | None = None, setter_position: int | None = None,
                late_set: bool | None = None) -> list[dict]:
    """Counts per evaluation code and percentages for one skill.
    group_by: player, team, match, skill_type, attack_code, set_number, start_zone, end_zone, end_cone,
    end_subzone, direction (start zone with end zone or cone), rotation (setter position of the team), phase
    (Serve / Reception = side-out / Transition) or reception_quality (the team's own reception before the touch,
    in the same possession: attack efficiency by pass quality); combine them with commas, e.g. "player,match".
    Zones and direction (court zones 1-9): start_zone is where the touch starts (attack: 4 = left, 3 = middle,
    2 = right, 9/8/7 = back row), end_zone where the ball lands, end_subzone A-D inside that zone. Some scouts
    record attack direction as end_cone (DataVolley cones, counted from the start zone) instead of end_zone; for
    serves and attacks end_zone is then computed from the end coordinates (end_zone_source 'coordinates' instead
    of 'code': less precise). start_x/start_y/end_x/end_y are court coordinates (x 0.5-3.5 across, y 0.5-6.5
    along the court, net at y 3.5), for heatmaps.
    Rally context: prev_skill (+ prev_evaluation, prev_skill_type) keeps touches that come after such a touch of
    the same team in the same possession, e.g. skill='Attack', prev_skill='Reception', prev_evaluation=['#'] =
    attacks after a perfect pass; prev_skill='Dig' = attacks after a dig. serve_type filters on the rally's serve
    (e.g. 'jump-float'), reception_evaluation on the rally's reception. setter_position = rotation (setter in
    P1-P6) of the touch's team. late_set = from 20 (12 in the fifth set).
    efficiency, in percent of total: serve (# + '+' + '/' - '=' - '-'), reception (# + '+' - '=' - '-'),
    attack and the other skills (# - '=' - '/')."""
    return await _tool(core.stats, skill=skill, group_by=group_by, player=player, team=team,
                       opponent=opponent, match_id=match_id, skill_type=skill_type, attack_code=attack_code,
                       start_zone=start_zone, end_zone=end_zone, end_cone=end_cone, end_subzone=end_subzone,
                       phase=phase, prev_skill=prev_skill, prev_evaluation=prev_evaluation,
                       prev_skill_type=prev_skill_type, serve_type=serve_type,
                       reception_evaluation=reception_evaluation, setter_position=setter_position, late_set=late_set)


@mcp.tool()
async def create_clip(action_ids: list[int] | None = None, tag_ids: list[int] | None = None,
                      caption: str | None = None, title: str | None = None, before_s: float = 4.0,
                      after_s: float = 4.0, slow_motion: bool = True,
                      skill_offsets: dict[str, float] | None = None) -> dict:
    """Make a video clip: one segment per action or tag, in the given order, from before_s seconds before
    to after_s after each touch. slow_motion plays the 2 s around the touch at half speed, muted.
    Each segment gets a caption (default: set, score, player, skill, evaluation; or your caption).
    skill_offsets shifts each skill's recorded time, e.g. {"Serve": -1.5} if serves were recorded late
    (default: the server's setting). For whole rallies use the serve with a longer after_s.
    Returns the clip with its status; asking again for the same clip returns the existing one."""
    return await _tool(core.create_clip, action_ids=action_ids, tag_ids=tag_ids, caption=caption, title=title,
                       before_s=before_s, after_s=after_s, slow_motion=slow_motion, skill_offsets=skill_offsets)


@mcp.tool()
async def scout_check(match_id: int | None = None) -> list[dict]:
    """Staff: inconsistencies in the scouting worth checking on video. One finding per rally
    with rule, set, score, message, the rally codes and action_ids (ready for create_clip). Rules:
    set_score_mismatch, zones_read_as_cones, winner_vs_last_touch, serve_reception_mismatch,
    serve_outcome_but_rally_continues, kill_then_play_continues, video_time_goes_back, video_time_gap,
    serve_types_mixed (a player with more than one serve type in a match: on purpose or a typo?)."""
    return await _tool(analysis.scout_check, match_id=match_id)


@mcp.tool()
async def phases(team: str | None = None, opponent: str | None = None, match_id: int | None = None,
                 group_by: str = "none") -> list[dict]:
    """Rally phases for one team (default: our team): side-out % (receiving rallies won), break-point %
    (serving rallies won), K1 = first attack after own reception (kill %, error %, efficiency), FBSO =
    receiving rallies won directly by that first attack, counter-attack (transition attacks while serving),
    side-out transition attacks, freeball rallies won. group_by: none, match, set or rotation (setter P1-P6)."""
    return await _tool(analysis.phases, team=team, opponent=opponent, match_id=match_id, group_by=group_by)


@mcp.tool()
async def opponent_report(opponent: str, last_matches: int | None = None) -> dict:
    """Material to prepare a match against an opponent, from our scouts of their matches: how_they_serve
    (servers, types, ace/error %, targets), their_reception_by_rotation and by_target_zone (where to serve),
    setting_distribution (first attack after reception by rotation and reception quality), attackers
    (kill/error/blocked %, start and end zones), block_and_defence. Write the booklet from these tables."""
    return await _tool(analysis.opponent_report, opponent=opponent, last_matches=last_matches)


@mcp.tool()
async def create_report(kind: str, team: str | None = None, season: str | None = None, match_id: int | None = None,
                        format: str = "pdf") -> dict:
    """Make a report (rendered on the server, like clips): kind 'season' (season report of a team: matches,
    players, team by match, rotations, attack after reception, setter distribution), 'setter_flow' (setter
    distribution arrows, pdf or png) or 'match' (match report of one match, needs match_id). team is a partial
    name (default: our team); season like '2026-27' (default: current). Returns {id, status}; poll get_report
    until status is 'ready' (usually under a minute), then share its url (valid 7 days). The same request with
    unchanged data returns the report already made."""
    return await _tool(reports.create_report, kind=kind, team=team, season=season, match_id=match_id, format=format)


@mcp.tool()
async def get_report(report_id: int) -> dict:
    """Status of a report; when 'ready' it has a url valid for a week. On 'failed', error says why."""
    return await _tool(reports.get_report, report_id=report_id)


@mcp.tool()
async def list_reports(mine: bool = False, limit: int = 20) -> list[dict]:
    """Reports already made, newest first."""
    return await _tool(reports.list_reports, mine=mine, limit=limit)


@mcp.tool()
async def get_clip(clip_id: int) -> dict:
    """Status of a clip; when status is 'ready' it has a url valid for a week."""
    return await _tool(core.get_clip, clip_id=clip_id)


@mcp.tool()
async def list_clips(mine: bool = False, limit: int = 50) -> list[dict]:
    """The clip library, newest first."""
    return await _tool(core.list_clips, mine=mine, limit=limit)


@mcp.tool()
async def query(sql: str, limit: int = 200) -> list[dict]:
    """Staff only: read-only SQL over the match data (tables: season, team, person, squad_member, match,
    match_player, video, rally, action, tag, clip, clip_action)."""
    return await _tool(core.run_query, sql=sql, limit=limit)


@mcp.tool()
async def list_drills(theme: str | None = None) -> list[dict]:
    """Staff: the drill library, by theme (Warmup, Passing and reception, Serve, Setting, Attack, Block, Defence,
    Transition, Sideout, Games and wash, Systems and rotations, Conditioning), with how often and when each drill
    was used. Drills come from the coach's training sessions."""
    return await _tool(library.list_drills, theme=theme)


@mcp.tool()
async def search_drills(text: str | None = None, theme: str | None = None, group: str | None = None,
                        season: str | None = None, limit: int = 20) -> list[dict]:
    """Staff: find drills by words in their name, description and coaching notes (e.g. "jump serve reception
    traffic light", "block transition"), optionally by theme, training day (group: Wednesday, Thursday, Sunday)
    or season. Returns each drill with its best-matching use; get_drill shows all uses and the diagrams."""
    return await _tool(library.search_drills, text=text, theme=theme, group=group, season=season, limit=limit)


@mcp.tool()
async def get_drill(drill: str, diagrams: int = 3) -> list:
    """Staff: one drill (id, slug or name) with every time it was used: date, training day, duration, summary,
    the coach's notes and links to the diagrams. The first `diagrams` diagrams are also returned as images so you
    can see them (0 for none). Player names are never stored: X marks a player in the notes."""
    result = await _tool(library.get_drill, drill=drill)
    return [result] + [Image(path=p) for p in library.image_paths(result, max(0, min(diagrams, 8)))]


@mcp.tool()
async def list_sessions(group: str | None = None, season: str | None = None, date_from: str | None = None,
                        date_to: str | None = None, drill: str | None = None) -> list[dict]:
    """Staff: training sessions (date, training day, head count, drills in order), filtered by training day,
    season, dates (YYYY-MM-DD) or a drill they contain."""
    return await _tool(library.list_sessions, group=group, season=season, date_from=date_from, date_to=date_to,
                       drill=drill)


@mcp.tool()
async def get_session(session_id: int, diagrams: int = 0) -> list:
    """Staff: one training session as planned: intro, then each block in order with its drill, duration, summary,
    the coach's notes and diagram links; `diagrams` > 0 also returns that many diagrams as images."""
    result = await _tool(library.get_session, session_id=session_id)
    return [result] + [Image(path=p) for p in library.image_paths(result, max(0, min(diagrams, 8)))]


@mcp.tool(description=(
    "Staff: draw a volleyball drill on a court and get a PNG link (valid 7 days) to give the user; the image is "
    "also returned so you can check it and redraw if something is off. Before drawing a drill from the library, "
    "look at the coach's own diagrams (get_drill / get_session with diagrams) and follow how he draws it. "
    + drawing.SPEC_HELP +
    " Zones: near side zone 4 is front-left as seen from the near baseline; the far side is seen by its own players."))
async def draw_drill(spec: dict) -> list:
    result = await _tool(drawing.draw_drill, spec=spec)
    return [result, Image(path=drawing.drawing_path(result["file"]))]


@mcp.tool()
async def usage_log(days: int = 7, who: str | None = None, event: str | None = None, tool: str | None = None,
                    group_by: str | None = None, errors_only: bool = False, limit: int = 200) -> list[dict]:
    """Admin only: who used volley-hub, when and for what. Without group_by, the events newest first; with
    group_by (comma-separated among user, tool, event, day, ip, status, path) counts, errors, average ms and
    first/last time. event: tool (MCP calls, with arguments), http (requests incl. rejected ones and media
    downloads), login (sign-ins; ok=false is a wrong access code), oauth_revoke. who matches the user name."""
    return await _tool(usage.usage_log, days=days, who=who, event=event, tool=tool, group_by=group_by,
                       errors_only=errors_only, limit=limit)


@mcp.tool(description=(
    "Admin only: DuckDB SQL over the event log of the last `days` days, in one table: " + usage.SCHEMA_DOC +
    " args is JSON (e.g. args->>'skill'). Example: select \"user\", tool, count(*) from events "
    "where event = 'tool' group by all order by 3 desc."))
async def log_query(sql: str, days: int = 30, limit: int = 500) -> list[dict]:
    return await _tool(usage.log_query, sql=sql, days=days, limit=limit)


# ------------------------------------------------------------------ REST

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.pool()
    for name in await anyio.to_thread.run_sync(migrate.migrate):
        print(f"applied migration {name}", flush=True)
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="volley-hub", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None,
              description="Match data, video and clips. Authenticate with `Authorization: Bearer <token>`.")
UserDep = Annotated[User, Depends(rest_user)]


class ClipRequest(BaseModel):
    action_ids: list[int] = Field(default_factory=list)
    tag_ids: list[int] = Field(default_factory=list)
    caption: str | None = None
    title: str | None = None
    before_s: float = 4.0
    after_s: float = 4.0
    slow_motion: bool = True
    skill_offsets: dict[str, float] | None = None


@app.get("/healthz", include_in_schema=False)
def healthz():
    db.fetch_one("select 1")
    return {"ok": True}


@app.get("/api/me")
def me(user: UserDep):
    return {"name": user.name, "role": user.role, "team_ids": list(user.team_ids)}


@app.get("/api/teams")
async def api_teams(user: UserDep):
    return await _run(core.list_teams, user)


@app.get("/api/matches")
async def api_matches(user: UserDep, team: str | None = None):
    return await _run(core.list_matches, user, team=team)


@app.get("/api/players")
async def api_players(user: UserDep, team: str | None = None):
    return await _run(core.list_players, user, team=team)


@app.get("/api/tags")
async def api_tags(user: UserDep, match_id: int | None = None):
    return await _run(core.list_tags, user, match_id=match_id)


@app.get("/api/actions")
async def api_actions(user: UserDep, player: str | None = None, team: str | None = None,
                      opponent: str | None = None, match_id: int | None = None, skill: str | None = None,
                      evaluation: Annotated[list[str] | None, Query()] = None, skill_type: str | None = None,
                      attack_code: str | None = None, set_number: int | None = None,
                      phase: str | None = None, start_zone: int | None = None, end_zone: int | None = None,
                      end_cone: int | None = None, end_subzone: str | None = None,
                      prev_skill: str | None = None, prev_evaluation: Annotated[list[str] | None, Query()] = None,
                      prev_skill_type: str | None = None, serve_type: str | None = None,
                      reception_evaluation: Annotated[list[str] | None, Query()] = None,
                      setter_position: int | None = None, late_set: bool | None = None, limit: int = 100):
    return await _run(core.search_actions, user, player=player, team=team, opponent=opponent,
                      match_id=match_id, skill=skill, evaluation=evaluation, skill_type=skill_type,
                      attack_code=attack_code, set_number=set_number, phase=phase, start_zone=start_zone,
                      end_zone=end_zone, end_cone=end_cone, end_subzone=end_subzone, prev_skill=prev_skill, prev_evaluation=prev_evaluation,
                      prev_skill_type=prev_skill_type, serve_type=serve_type,
                      reception_evaluation=reception_evaluation, setter_position=setter_position, late_set=late_set, limit=limit)


@app.get("/api/stats")
async def api_stats(user: UserDep, skill: str, group_by: str = "player", player: str | None = None,
                    team: str | None = None, opponent: str | None = None, match_id: int | None = None,
                    skill_type: str | None = None, attack_code: str | None = None, start_zone: int | None = None,
                    end_zone: int | None = None, end_cone: int | None = None, end_subzone: str | None = None,
                    phase: str | None = None, prev_skill: str | None = None, prev_evaluation: Annotated[list[str] | None, Query()] = None,
                    prev_skill_type: str | None = None, serve_type: str | None = None,
                    reception_evaluation: Annotated[list[str] | None, Query()] = None,
                    setter_position: int | None = None, late_set: bool | None = None):
    return await _run(core.stats, user, skill=skill, group_by=group_by, player=player, team=team,
                      opponent=opponent, match_id=match_id, skill_type=skill_type, attack_code=attack_code,
                      start_zone=start_zone, end_zone=end_zone, end_cone=end_cone, end_subzone=end_subzone,
                      phase=phase, prev_skill=prev_skill, prev_evaluation=prev_evaluation,
                      prev_skill_type=prev_skill_type, serve_type=serve_type,
                      reception_evaluation=reception_evaluation, setter_position=setter_position, late_set=late_set)


@app.get("/api/scout-check")
async def api_scout_check(user: UserDep, match_id: int | None = None):
    return await _run(analysis.scout_check, user, match_id=match_id)


@app.get("/api/phases")
async def api_phases(user: UserDep, team: str | None = None, opponent: str | None = None,
                     match_id: int | None = None, group_by: str = "none"):
    return await _run(analysis.phases, user, team=team, opponent=opponent, match_id=match_id, group_by=group_by)


@app.get("/api/opponent-report")
async def api_opponent_report(user: UserDep, opponent: str, last_matches: int | None = None):
    return await _run(analysis.opponent_report, user, opponent=opponent, last_matches=last_matches)


class ReportRequest(BaseModel):
    kind: str
    team: str | None = None
    season: str | None = None
    match_id: int | None = None
    format: str = "pdf"


@app.post("/api/reports")
async def api_create_report(user: UserDep, req: ReportRequest):
    return await _run(reports.create_report, user, **req.model_dump())


@app.get("/api/reports")
async def api_reports(user: UserDep, mine: bool = False, limit: int = 20):
    return await _run(reports.list_reports, user, mine=mine, limit=limit)


@app.get("/api/reports/{report_id}")
async def api_report(user: UserDep, report_id: int):
    return await _run(reports.get_report, user, report_id=report_id)


@app.get("/api/drills")
async def api_drills(user: UserDep, text: str | None = None, theme: str | None = None, group: str | None = None,
                     season: str | None = None, limit: int = 20):
    return await _run(library.search_drills, user, text=text, theme=theme, group=group, season=season, limit=limit)


@app.get("/api/drills/{drill}")
async def api_drill(user: UserDep, drill: str):
    return await _run(library.get_drill, user, drill=drill)


@app.get("/api/sessions")
async def api_sessions(user: UserDep, group: str | None = None, season: str | None = None,
                       date_from: str | None = None, date_to: str | None = None, drill: str | None = None):
    return await _run(library.list_sessions, user, group=group, season=season, date_from=date_from,
                      date_to=date_to, drill=drill)


@app.get("/api/sessions/{session_id}")
async def api_session(user: UserDep, session_id: int):
    return await _run(library.get_session, user, session_id=session_id)


@app.post("/api/clips")
async def api_create_clip(user: UserDep, req: ClipRequest):
    return await _run(core.create_clip, user, **req.model_dump())


@app.get("/api/clips")
async def api_clips(user: UserDep, mine: bool = False, limit: int = 50):
    return await _run(core.list_clips, user, mine=mine, limit=limit)


@app.get("/api/clips/{clip_id}")
async def api_clip(user: UserDep, clip_id: int):
    return await _run(core.get_clip, user, clip_id)


# ------------------------------------------------------------------ signed media (no token, signed URL)

def _require_signature(request: Request) -> None:
    q = request.query_params
    if not auth.check_signature(request.url.path, q.get("exp"), q.get("sig")):
        raise HTTPException(403, "link expired or invalid")


def _safe_file(base: Path, name: str) -> Path:
    path = (base / name).resolve()
    if base.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "not found")
    return path


@app.get("/media/clips/{clip_id}.mp4", include_in_schema=False)
def media_clip(request: Request, clip_id: int):
    _require_signature(request)
    row = db.fetch_one("select storage_key from clip where id = %s and status = 'ready'", (clip_id,))
    if row is None:
        raise HTTPException(404, "not found")
    return FileResponse(_safe_file(config.CLIP_DIR, row["storage_key"]), media_type="video/mp4")


@app.get("/media/reports/{name}", include_in_schema=False)
def media_report(request: Request, name: str):
    _require_signature(request)
    row = db.fetch_one("select storage_key, format from report where storage_key = %s and status = 'ready'", (name,))
    if row is None:
        raise HTTPException(404, "not found")
    media = "application/pdf" if row["format"] == "pdf" else "image/png"
    return FileResponse(_safe_file(config.REPORT_DIR, row["storage_key"]), media_type=media, filename=name)


@app.get("/media/drawings/{name}", include_in_schema=False)
def media_drawing(request: Request, name: str):
    _require_signature(request)
    return FileResponse(_safe_file(config.DRAWING_DIR, name), media_type="image/png")


@app.get("/media/library/{path:path}", include_in_schema=False)
def media_library(request: Request, path: str):
    q = request.query_params
    if not auth.check_signature(request.scope["path"], q.get("exp"), q.get("sig")):
        raise HTTPException(403, "link expired or invalid")
    file = _safe_file(config.LIBRARY_DIR, path)
    media = {".png": "image/png", ".pdf": "application/pdf"}.get(file.suffix.lower(), "application/octet-stream")
    return FileResponse(file, media_type=media)


@app.get("/media/videos/{match_id}", include_in_schema=False)
def media_video(request: Request, match_id: int):
    _require_signature(request)
    row = db.fetch_one("select storage_key from video where match_id = %s order by id limit 1", (match_id,))
    if row is None or not row["storage_key"]:
        raise HTTPException(404, "not found")
    return FileResponse(_safe_file(config.VIDEO_DIR, row["storage_key"]), media_type="video/mp4")


@app.get("/", include_in_schema=False)
def index():
    return {"name": "volley-hub", "mcp": f"{config.PUBLIC_URL}/mcp", "openapi": f"{config.PUBLIC_URL}/openapi.json",
            "auth": "Authorization: Bearer <token>"}


# OAuth endpoints and the sign-in page, then MCP last: its app serves /mcp; everything above takes precedence.
app.router.routes.extend(oauth.routes())
_public_host = urlparse(config.PUBLIC_URL).netloc
app.mount("/", mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=[_public_host, "127.0.0.1:*", "localhost:*", "api:*"],
        allowed_origins=[config.PUBLIC_URL, "http://127.0.0.1:*", "http://localhost:*"],
    ),
))
app = AuthMiddleware(app)  # type: ignore[assignment]
