"""HTTP entry point: MCP server at /mcp, REST API (schema at /openapi.json), signed media files. No web pages."""

import contextvars
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import anyio
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from . import auth, config, core, db, migrate, oauth
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
        if scope["type"] != "http" or scope["path"] == "/" or scope["path"].startswith(PUBLIC_PREFIXES):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        user = await anyio.to_thread.run_sync(_user_for_bearer, auth.bearer(headers))
        if user is None:
            body = b'{"detail":"missing or invalid bearer token"}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", _WWW_AUTH)]})
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})["user"] = user
        token = _current_user.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_user.reset(token)


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
    return await anyio.to_thread.run_sync(lambda: fn(user, **kwargs))


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
                         limit: int = 100) -> list[dict]:
    """Find touches. All filters optional and combined with AND. player/team/opponent are partial names.
    evaluation is a list of codes, e.g. ['=', '/'] for errors and blocked attacks.
    phase: Serve, Reception (side-out) or Transition. Results with video have a video_url: the match video starting 4 s before the touch (signed link, a file, no page). To watch or share a touch, prefer create_clip.
    Zones and direction (court zones 1-9): start_zone is where the touch starts (attack: 4 = left, 3 = middle,
    2 = right, 9/8/7 = back row), end_zone where the ball lands, end_subzone A-D inside that zone. Some scouts
    record attack direction as end_cone instead of end_zone (DataVolley cones, counted from the start zone):
    when end_zone is empty, use end_cone."""
    return await _tool(core.search_actions, player=player, team=team, opponent=opponent, match_id=match_id,
                       skill=skill, evaluation=evaluation, skill_type=skill_type, attack_code=attack_code,
                       set_number=set_number, phase=phase, start_zone=start_zone, end_zone=end_zone,
                       end_cone=end_cone, end_subzone=end_subzone, limit=limit)


@mcp.tool()
async def stats(skill: str, group_by: str = "player", player: str | None = None, team: str | None = None,
                opponent: str | None = None, match_id: int | None = None, skill_type: str | None = None,
                attack_code: str | None = None, start_zone: int | None = None, end_zone: int | None = None,
                end_cone: int | None = None, end_subzone: str | None = None) -> list[dict]:
    """Counts per evaluation code and percentages for one skill.
    group_by: player, team, match, skill_type, attack_code, set_number, start_zone, end_zone, end_cone,
    end_subzone or direction (start zone with end zone or cone).
    Zones and direction (court zones 1-9): start_zone is where the touch starts (attack: 4 = left, 3 = middle,
    2 = right, 9/8/7 = back row), end_zone where the ball lands, end_subzone A-D inside that zone. Some scouts
    record attack direction as end_cone instead of end_zone (DataVolley cones, counted from the start zone):
    when end_zone is empty, use end_cone.
    efficiency = (# minus = minus /) / total, in percent (for attack: kill minus errors minus blocked)."""
    return await _tool(core.stats, skill=skill, group_by=group_by, player=player, team=team,
                       opponent=opponent, match_id=match_id, skill_type=skill_type, attack_code=attack_code,
                       start_zone=start_zone, end_zone=end_zone, end_cone=end_cone, end_subzone=end_subzone)


@mcp.tool()
async def create_clip(action_ids: list[int] | None = None, tag_ids: list[int] | None = None,
                      caption: str | None = None, title: str | None = None, before_s: float = 4.0,
                      after_s: float = 4.0, slow_motion: bool = True) -> dict:
    """Make a video clip: one segment per action or tag, in the given order, from before_s seconds before
    to after_s after each touch. slow_motion plays the 2 s around the touch at half speed, muted.
    Each segment gets a caption (default: set, score, player, skill, evaluation; or your caption).
    Returns the clip with its status; asking again for the same clip returns the existing one."""
    return await _tool(core.create_clip, action_ids=action_ids, tag_ids=tag_ids, caption=caption, title=title,
                       before_s=before_s, after_s=after_s, slow_motion=slow_motion)


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
                      end_cone: int | None = None, end_subzone: str | None = None, limit: int = 100):
    return await _run(core.search_actions, user, player=player, team=team, opponent=opponent,
                      match_id=match_id, skill=skill, evaluation=evaluation, skill_type=skill_type,
                      attack_code=attack_code, set_number=set_number, phase=phase, start_zone=start_zone,
                      end_zone=end_zone, end_cone=end_cone, end_subzone=end_subzone, limit=limit)


@app.get("/api/stats")
async def api_stats(user: UserDep, skill: str, group_by: str = "player", player: str | None = None,
                    team: str | None = None, opponent: str | None = None, match_id: int | None = None,
                    skill_type: str | None = None, attack_code: str | None = None, start_zone: int | None = None,
                    end_zone: int | None = None, end_cone: int | None = None, end_subzone: str | None = None):
    return await _run(core.stats, user, skill=skill, group_by=group_by, player=player, team=team,
                      opponent=opponent, match_id=match_id, skill_type=skill_type, attack_code=attack_code,
                      start_zone=start_zone, end_zone=end_zone, end_cone=end_cone, end_subzone=end_subzone)


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
