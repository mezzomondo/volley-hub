"""OAuth 2.1 authorization server for MCP connectors.

The protocol endpoints (/.well-known/*, /authorize, /token, /register, /revoke) come from the MCP SDK,
which checks PKCE, redirect URIs, code expiry and client secrets. This module stores clients, codes and
tokens in Postgres (hashed) and adds the one page OAuth needs: /login, where people sign in with
their personal access code (the same code that works as a bearer token).
"""

import hashlib
import html
import json
import secrets
import time
import uuid
from urllib.parse import parse_qs, urlparse

import anyio
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.routes import build_metadata, cors_middleware, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import auth, config, db, log

SCOPE = "volley-hub"
PENDING_TTL_S = 15 * 60
CODE_TTL_S = 5 * 60
ACCESS_TTL_S = 3600
REFRESH_TTL_S = 90 * 24 * 3600

RESOURCE = f"{config.PUBLIC_URL}/mcp"
_OK_RESOURCES = {RESOURCE, RESOURCE + "/", config.PUBLIC_URL, config.PUBLIC_URL + "/"}
_BAD_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob", "ftp", "ws", "wss"}
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}

PUBLIC_PATHS = ("/.well-known/", "/authorize", "/token", "/register", "/revoke", "/login")
RESOURCE_METADATA_URL = f"{config.PUBLIC_URL}/.well-known/oauth-protected-resource/mcp"


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _db(fn, *args):
    return await anyio.to_thread.run_sync(lambda: fn(*args))


def _check_redirect_uri(uri: str) -> None:
    u = urlparse(uri)
    scheme = u.scheme.lower()
    if not scheme or scheme in _BAD_SCHEMES or u.fragment:
        raise RegistrationError("invalid_redirect_uri", f"redirect URI not allowed: {uri}")
    if scheme == "http" and (u.hostname or "") not in _LOOPBACK:
        raise RegistrationError("invalid_redirect_uri", "http redirect URIs are only allowed on localhost")


# ------------------------------------------------------------------ storage (sync, run in threads)

def _cleanup(c) -> None:
    c.execute("delete from oauth_pending where expires_at < now()")
    c.execute("delete from oauth_code where expires_at < now()")
    c.execute("delete from oauth_token where expires_at < now()")


def _issue(c, client_id: str, user_id: int, scopes: list[str], resource: str | None, family: str) -> OAuthToken:
    access, refresh = "vha_" + secrets.token_urlsafe(32), "vhr_" + secrets.token_urlsafe(32)
    for token, kind, ttl in ((access, "access", ACCESS_TTL_S), (refresh, "refresh", REFRESH_TTL_S)):
        c.execute("""insert into oauth_token (token_hash, kind, family, client_id, user_id, scopes, resource, expires_at)
                     values (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))""",
                  (_h(token), kind, family, client_id, user_id, scopes, resource, ttl))
    return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL_S,
                      refresh_token=refresh, scope=" ".join(scopes) or None)


def user_for_access_token(token: str) -> auth.User | None:
    row = db.fetch_one("""
        select u.id, u.name, u.role, u.team_ids from oauth_token t join app_user u on u.id = t.user_id
        where t.token_hash = %s and t.kind = 'access' and t.expires_at > now() and u.active""", (_h(token),))
    if row is None:
        return None
    return auth.User(row["id"], row["name"], row["role"], tuple(row["team_ids"] or ()))


def revoke_user_sessions(c, user_id: int) -> int:
    return c.execute("delete from oauth_token where user_id = %s", (user_id,)).rowcount


# ------------------------------------------------------------------ provider for the SDK handlers

class Provider:
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = await _db(db.fetch_one, "select info from oauth_client where client_id = %s", (client_id,))
        return OAuthClientInformationFull.model_validate(row["info"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            _check_redirect_uri(str(uri))

        def save():
            with db.conn() as c:
                c.execute("insert into oauth_client (client_id, info) values (%s, %s)",
                          (client_info.client_id, json.dumps(client_info.model_dump(mode="json", exclude_none=True))))
        await _db(save)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource not in _OK_RESOURCES:
            raise AuthorizeError("invalid_target", "unknown resource")
        pending = secrets.token_urlsafe(32)

        def save():
            with db.conn() as c:
                _cleanup(c)
                c.execute("""insert into oauth_pending (id_hash, client_id, params, expires_at)
                             values (%s, %s, %s, now() + make_interval(secs => %s))""",
                          (_h(pending), client.client_id, json.dumps(params.model_dump(mode="json")), PENDING_TTL_S))
        await _db(save)
        return f"{config.PUBLIC_URL}/login?req={pending}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, code: str) -> AuthorizationCode | None:
        row = await _db(db.fetch_one, """
            select user_id, params, extract(epoch from expires_at) as exp from oauth_code
            where code_hash = %s and client_id = %s""", (_h(code), client.client_id))
        if row is None:
            return None
        p = row["params"]
        return AuthorizationCode(code=code, scopes=p.get("scopes") or [SCOPE], expires_at=float(row["exp"]),
                                 client_id=client.client_id, code_challenge=p["code_challenge"],
                                 redirect_uri=p["redirect_uri"],
                                 redirect_uri_provided_explicitly=p["redirect_uri_provided_explicitly"],
                                 resource=p.get("resource"), subject=str(row["user_id"]))

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          code: AuthorizationCode) -> OAuthToken:
        def run():
            with db.conn() as c:
                row = c.execute("delete from oauth_code where code_hash = %s and client_id = %s returning user_id",
                                (_h(code.code), client.client_id)).fetchone()
                if row is None:  # already used
                    raise TokenError("invalid_grant", "authorization code already used")
                return _issue(c, client.client_id, row["user_id"], code.scopes, code.resource, uuid.uuid4().hex)
        return await _db(run)

    async def load_refresh_token(self, client: OAuthClientInformationFull, token: str) -> RefreshToken | None:
        row = await _db(db.fetch_one, """
            select t.user_id, t.scopes, t.resource, extract(epoch from t.expires_at)::bigint as exp
            from oauth_token t join app_user u on u.id = t.user_id
            where t.token_hash = %s and t.kind = 'refresh' and t.client_id = %s and u.active""",
            (_h(token), client.client_id))
        if row is None:
            return None
        return RefreshToken(token=token, client_id=client.client_id, scopes=list(row["scopes"]),
                            expires_at=int(row["exp"]), resource=row["resource"], subject=str(row["user_id"]))

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        def run():
            with db.conn() as c:
                row = c.execute("""delete from oauth_token where token_hash = %s and kind = 'refresh'
                                   returning family, user_id, resource""", (_h(token.token),)).fetchone()
                if row is None:
                    raise TokenError("invalid_grant", "refresh token already used")
                c.execute("delete from oauth_token where family = %s", (row["family"],))
                return _issue(c, client.client_id, row["user_id"], scopes or token.scopes, row["resource"],
                              row["family"])
        return await _db(run)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = await _db(db.fetch_one, """
            select client_id, user_id, scopes, resource, extract(epoch from expires_at)::bigint as exp
            from oauth_token where token_hash = %s and kind = 'access' and expires_at > now()""", (_h(token),))
        if row is None:
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=list(row["scopes"]),
                           expires_at=int(row["exp"]), resource=row["resource"], subject=str(row["user_id"]))

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        def run():
            with db.conn() as c:
                row = c.execute("select family from oauth_token where token_hash = %s", (_h(token.token),)).fetchone()
                if row:
                    c.execute("delete from oauth_token where family = %s", (row["family"],))
        await _db(run)


# ------------------------------------------------------------------ the login page

_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
}


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>volley-hub sign-in</title>
<style>
body{{margin:0;font:16px system-ui,sans-serif;background:#f4f4f5;color:#18181b}}
main{{max-width:420px;margin:12vh auto;padding:24px;background:#fff;border-radius:12px;box-shadow:0 1px 3px #0002}}
h1{{font-size:20px;margin:0 0 12px}} p{{line-height:1.45}} .muted{{color:#52525b;font-size:14px}}
input{{width:100%;box-sizing:border-box;font:inherit;padding:10px;border:1px solid #a1a1aa;border-radius:8px}}
button{{margin-top:12px;width:100%;font:inherit;padding:10px;border:0;border-radius:8px;background:#18181b;color:#fff}}
.err{{color:#b91c1c}}
@media (prefers-color-scheme: dark){{body{{background:#18181b;color:#f4f4f5}}main{{background:#27272a}}
.muted{{color:#a1a1aa}}input{{background:#18181b;color:#f4f4f5}}button{{background:#f4f4f5;color:#18181b}}}}
</style></head><body><main>{body}</main></body></html>""", status_code=status, headers=_HEADERS)


def _load_pending(req: str) -> dict | None:
    return db.fetch_one("""
        select p.client_id, p.params, c.info from oauth_pending p join oauth_client c using (client_id)
        where p.id_hash = %s and p.expires_at > now()""", (_h(req),))


def _form(req: str, pending: dict, error: str = "") -> HTMLResponse:
    info, params = pending["info"], pending["params"]
    app_name = html.escape(info.get("client_name") or "An app")
    target = html.escape(urlparse(params["redirect_uri"]).hostname or params["redirect_uri"])
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return _page(f"""<h1>Connect to volley-hub</h1>
<p><b>{app_name}</b> wants to use your volley-hub data (sign-in goes back to <b>{target}</b>).</p>
<form method="post" action="/login">
<input type="hidden" name="req" value="{html.escape(req)}">
<label for="code" class="muted">Your personal access code</label>
<input id="code" name="code" type="password" autocomplete="current-password" required autofocus>
{err}<button type="submit">Connect</button></form>
<p class="muted">No code? Ask the club staff. Never paste it anywhere else.</p>""")


async def login_get(request: Request) -> Response:
    req = request.query_params.get("req", "")
    pending = await _db(_load_pending, req) if req else None
    if pending is None:
        return _page("<h1>Link expired</h1><p>Start the connection again from your AI app.</p>", 400)
    return _form(req, pending)


async def login_post(request: Request) -> Response:
    body = (await request.body())[:8192].decode(errors="replace")
    form = {k: v[0] for k, v in parse_qs(body).items()}
    req, code = form.get("req", ""), form.get("code", "").strip()
    pending = await _db(_load_pending, req) if req else None
    if pending is None:
        return _page("<h1>Link expired</h1><p>Start the connection again from your AI app.</p>", 400)
    user = await _db(auth.user_for_token, code) if code else None
    if user is None:
        log.event("login", ok=False, client=pending["client_id"],
                  ip=(request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or None)
        await anyio.sleep(1)  # slow down guessing
        return _form(req, pending, "That code is not valid.")
    params = pending["params"]
    auth_code = secrets.token_urlsafe(32)

    def save():
        with db.conn() as c:
            gone = c.execute("delete from oauth_pending where id_hash = %s returning 1", (_h(req),)).fetchone()
            if gone is None:
                return False
            c.execute("""insert into oauth_code (code_hash, client_id, user_id, params, expires_at)
                         values (%s, %s, %s, %s, now() + make_interval(secs => %s))""",
                      (_h(auth_code), pending["client_id"], user.id, json.dumps(params), CODE_TTL_S))
            return True
    if not await _db(save):
        return _page("<h1>Link expired</h1><p>Start the connection again from your AI app.</p>", 400)
    log.event("login", ok=True, user=user.name, role=user.role, client=pending["client_id"],
              ip=(request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or None)
    location = construct_redirect_uri(params["redirect_uri"], code=auth_code, state=params.get("state"))
    return RedirectResponse(location, status_code=303, headers={"Cache-Control": "no-store"})


async def revoke(request: Request) -> Response:
    """RFC 7009 revocation for public clients too (the SDK handler insists on a client_secret)."""
    body = (await request.body())[:8192].decode(errors="replace")
    token = (parse_qs(body).get("token") or [""])[0]

    def run():
        with db.conn() as c:
            row = c.execute("select family from oauth_token where token_hash = %s", (_h(token),)).fetchone()
            if row:
                c.execute("delete from oauth_token where family = %s", (row["family"],))
                log.event("oauth_revoke", family=str(row["family"])[:8])
    if token:
        await _db(run)
    return Response(status_code=200, headers={"Cache-Control": "no-store"})


def routes() -> list[Route]:
    issuer = AnyHttpUrl(config.PUBLIC_URL)
    registration = ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE])
    rs = create_auth_routes(provider=Provider(), issuer_url=issuer, client_registration_options=registration,
                            revocation_options=RevocationOptions(enabled=False))
    # Our own metadata document: same as the SDK's plus public clients ("none") and our /revoke.
    metadata = build_metadata(issuer, None, registration, RevocationOptions(enabled=False))
    metadata.token_endpoint_auth_methods_supported = ["none", "client_secret_post", "client_secret_basic"]
    metadata.revocation_endpoint = AnyHttpUrl(f"{config.PUBLIC_URL}/revoke")
    metadata.revocation_endpoint_auth_methods_supported = ["none"]
    rs = [r for r in rs if r.path != "/.well-known/oauth-authorization-server"]
    rs.append(Route("/.well-known/oauth-authorization-server",
                    endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
                    methods=["GET", "OPTIONS"]))
    rs.append(Route("/revoke", revoke, methods=["POST"]))
    # RFC 9728 metadata at /.well-known/oauth-protected-resource/mcp, and the same document at the
    # root path, where some clients look first.
    prm = create_protected_resource_routes(resource_url=AnyHttpUrl(RESOURCE), authorization_servers=[issuer],
                                           scopes_supported=[SCOPE], resource_name="volley-hub")
    rs += prm
    rs.append(Route("/.well-known/oauth-protected-resource", endpoint=prm[0].endpoint, methods=["GET", "OPTIONS"]))
    rs += [Route("/login", login_get, methods=["GET"]), Route("/login", login_post, methods=["POST"])]
    return rs
