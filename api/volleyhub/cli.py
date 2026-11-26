"""Admin commands.

    volleyhub create-user --name "Casey Hudson" --role player --team "Onyx NVL"
    volleyhub list-users
    volleyhub revoke --name "Casey Hudson"
    volleyhub rotate-token --name "Casey Hudson"
"""

import argparse
import sys

from . import auth, db


def _team_ids(names: list[str]) -> list[int]:
    ids = []
    for n in names:
        row = db.fetch_one("select id from team where name = %s", (n,))
        if row is None:
            sys.exit(f"unknown team: {n}")
        ids.append(row["id"])
    return ids


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="volleyhub")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("--name", required=True)
    c.add_argument("--role", required=True, choices=["player", "management", "staff"])
    c.add_argument("--team", action="append", default=[], help="repeatable; required for players")
    sub.add_parser("list-users")
    r = sub.add_parser("revoke")
    r.add_argument("--name", required=True)
    t = sub.add_parser("rotate-token")
    t.add_argument("--name", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "create-user":
        if args.role == "player" and not args.team:
            sys.exit("players need at least one --team")
        token = auth.new_token()
        with db.conn() as conn:
            conn.execute("insert into app_user (name, role, team_ids, token_hash) values (%s, %s, %s, %s)",
                         (args.name, args.role, _team_ids(args.team), auth.hash_token(token)))
        print(f"user {args.name} ({args.role}) created. Token (shown only now):\n{token}")
    elif args.cmd == "list-users":
        for u in db.fetch_all("""
            select u.name, u.role, u.active, coalesce(string_agg(t.name, ', '), '') as teams
            from app_user u left join team t on t.id = any(u.team_ids)
            group by u.id order by u.name"""):
            print(f"{u['name']:<30} {u['role']:<11} {'active' if u['active'] else 'revoked':<8} {u['teams']}")
    elif args.cmd == "revoke":
        with db.conn() as conn:
            n = conn.execute("update app_user set active = false where name = %s", (args.name,)).rowcount
        print("revoked" if n else "no such user")
    elif args.cmd == "rotate-token":
        token = auth.new_token()
        with db.conn() as conn:
            n = conn.execute("update app_user set token_hash = %s, active = true where name = %s",
                             (auth.hash_token(token), args.name)).rowcount
        print(f"new token for {args.name} (shown only now):\n{token}" if n else "no such user")


if __name__ == "__main__":
    main()
