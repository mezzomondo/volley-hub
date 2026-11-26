"""Schema migrations: db/schema.sql is the baseline; files in migrations/ are applied once, in order."""

from pathlib import Path

from . import db

MIGRATIONS = Path(__file__).with_name("migrations")
_LOCK = 0x766F6C6C  # advisory lock id, so two processes never migrate at once


def migrate() -> list[str]:
    applied = []
    with db.conn() as c:
        c.execute("select pg_advisory_lock(%s)", (_LOCK,))
        try:
            c.execute("create table if not exists schema_migration "
                      "(name text primary key, applied_at timestamptz not null default now())")
            c.commit()
            done = {r["name"] for r in c.execute("select name from schema_migration").fetchall()}
            for f in sorted(MIGRATIONS.glob("*.sql")):
                if f.name in done:
                    continue
                with c.transaction():
                    c.execute(f.read_text())
                    c.execute("insert into schema_migration (name) values (%s)", (f.name,))
                applied.append(f.name)
        finally:
            c.execute("select pg_advisory_unlock(%s)", (_LOCK,))
            c.commit()
    return applied
