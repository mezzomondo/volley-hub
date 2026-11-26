"""Admin investigation tools over the event log (log.py), queried with DuckDB.

The day files are loaded into an in-memory DuckDB table `events`; then file and network access is switched off
and the configuration locked, so admin SQL can only see that table."""

import datetime as dt

import duckdb

from . import config
from .auth import User
from .core import Forbidden

COLUMNS = {
    "ts": "TIMESTAMP", "event": "VARCHAR", "user": "VARCHAR", "role": "VARCHAR", "tool": "VARCHAR",
    "args": "JSON", "ms": "BIGINT", "ok": "BOOLEAN", "error": "VARCHAR", "method": "VARCHAR", "path": "VARCHAR",
    "status": "INTEGER", "ip": "VARCHAR", "agent": "VARCHAR", "client": "VARCHAR", "family": "VARCHAR",
}
SCHEMA_DOC = ("events(" + ", ".join(f"{k} {v}" for k, v in COLUMNS.items()) + "). "
              "event is http | tool | login | oauth_revoke; ts is UTC.")
GROUPS = {"user": '"user"', "tool": "tool", "event": "event", "day": "cast(ts as date)", "ip": "ip",
          "status": "status", "path": "path"}


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise Forbidden("admin only")


def _connect(days: int) -> duckdb.DuckDBPyConnection:
    days = max(1, min(int(days), 400))
    today = dt.datetime.now(dt.UTC).date()
    folder = config.LOG_DIR / "events"
    files = [str(p) for i in range(days) if (p := folder / f"{today - dt.timedelta(days=i)}.jsonl").exists()]
    con = duckdb.connect(":memory:")
    cols = "{" + ", ".join(f"'{k}': '{v}'" for k, v in COLUMNS.items()) + "}"
    if files:
        con.execute(f"create table events as select * from read_json(?, format='newline_delimited', "
                    f"columns={cols}, ignore_errors=true)", [files])
    else:
        con.execute("create table events (" + ", ".join(f'"{k}" {v}' for k, v in COLUMNS.items()) + ")")
    con.execute("set enable_external_access = false")
    con.execute("set lock_configuration = true")
    return con


def _rows(cur, limit: int) -> list[dict]:
    names = [d[0] for d in cur.description] if cur.description else []
    out = []
    for row in cur.fetchmany(max(1, min(int(limit), 5000))):
        out.append({n: (v.isoformat() if hasattr(v, "isoformat") else v) for n, v in zip(names, row) if v is not None})
    return out


def usage_log(user: User, days: int = 7, who: str | None = None, event: str | None = None,
              tool: str | None = None, group_by: str | None = None, errors_only: bool = False,
              limit: int = 200) -> list[dict]:
    _require_admin(user)
    con = _connect(days)
    where, params = ["true"], []
    if who:
        where.append('"user" ilike ?'); params.append(f"%{who}%")
    if event:
        where.append("event = ?"); params.append(event)
    if tool:
        where.append("tool = ?"); params.append(tool)
    if errors_only:
        where.append("(ok = false or status >= 400)")
    if not event and not tool:  # every MCP call is also an http line to /mcp: keep the tool line
        where.append("not (event = 'http' and path like '/mcp%' and status < 400)")
    cond = " and ".join(where)
    if group_by:
        keys = [k.strip() for k in group_by.split(",") if k.strip()]
        bad = [k for k in keys if k not in GROUPS]
        if bad:
            raise ValueError(f"group_by must be among {', '.join(GROUPS)}")
        sel = ", ".join(f"{GROUPS[k]} as {k}" for k in keys)
        sql = (f"select {sel}, count(*) as n, count(*) filter (where ok = false or status >= 400) as errors, "
               f"round(avg(ms)) as avg_ms, min(ts) as first, max(ts) as last from events where {cond} "
               f"group by all order by n desc")
    else:
        sql = (f"select ts, event, \"user\", role, tool, args, ms, ok, error, method, path, status, ip, agent "
               f"from events where {cond} order by ts desc")
    return _rows(con.execute(sql, params), limit)


def log_query(user: User, sql: str, days: int = 30, limit: int = 500) -> list[dict]:
    _require_admin(user)
    return _rows(_connect(days).execute(sql), limit)
