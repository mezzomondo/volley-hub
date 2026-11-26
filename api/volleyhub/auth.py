"""API tokens and signed media links."""

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from . import config, db


@dataclass(frozen=True)
class User:
    id: int
    name: str
    role: str                      # player | management | staff
    team_ids: tuple[int, ...] = field(default_factory=tuple)

    @property
    def sees_all_teams(self) -> bool:
        return self.role in ("management", "staff")

    @property
    def is_staff(self) -> bool:
        return self.role == "staff"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return "vh_" + secrets.token_urlsafe(32)


def user_for_token(token: str | None) -> User | None:
    if not token:
        return None
    row = db.fetch_one(
        "select id, name, role, team_ids from app_user where token_hash = %(h)s and active",
        {"h": hash_token(token)},
    )
    if row is None:
        return None
    return User(row["id"], row["name"], row["role"], tuple(row["team_ids"] or ()))


def bearer(headers: dict[str, str]) -> str | None:
    value = headers.get("authorization", "")
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


# ------------------------------------------------------------- signed links

def _sig(path: str, exp: int) -> str:
    return hmac.new(config.SIGNING_KEY, f"{path}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def signed_url(path: str, extra: dict | None = None, ttl: int | None = None) -> str:
    exp = int(time.time()) + (ttl or config.MEDIA_LINK_TTL_S)
    query = dict(extra or {})
    query.update({"exp": exp, "sig": _sig(path, exp)})
    return f"{config.PUBLIC_URL}{path}?{urlencode(query)}"


def check_signature(path: str, exp: str | None, sig: str | None) -> bool:
    if not exp or not sig or not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(_sig(path, int(exp)), sig)
