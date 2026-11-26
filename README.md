# volley-hub

A small, self-hosted hub for a volleyball club's match data and video.

Coaches, players and club staff can search scouted actions, see stats and
build their own video clips from the AI assistant they already use, through
[MCP](https://modelcontextprotocol.io) (Claude, ChatGPT, Gemini, Open WebUI,
LibreChat, local models...). There is no web app to learn: the conversation is
the interface, and the hub only hands back data and links to clips and video.
A plain REST/OpenAPI interface does the same for scripts. No vendor lock-in: everything runs in
Docker Compose on one VM.

Status: early but working: schema, .dvw ingest, REST API, MCP server, clip worker and AWS deployment (see `docs/deploy.md`). Next: OAuth sign-in, so web assistants (claude.ai, ChatGPT) can connect too.

## Architecture

```
  any MCP client ────┬──> api (REST + MCP) ──> Postgres
  scripts/OpenAPI ───┘          │
                                └──> worker (ffmpeg) ──> clip library
ingest (R + datavolley): .dvw + video ──> Postgres + video store
backups (pg_dump + clips) ──> S3
```

| Component | What it does |
|---|---|
| `db/` | Postgres schema: seasons, teams, people, per-match shirt numbers, rallies, actions, videos, clips, staff notes. Row-level security for access levels. |
| `ingest/` | Reads DataVolley `.dvw` files with [datavolley](https://github.com/openvolley/datavolley) and loads them; registers match videos. |
| `api/` | REST + MCP server: search actions, stats, create/find clips. |
| `api/volleyhub/worker.py` | ffmpeg jobs: cut clips from match video, captions, slow motion; stores them in the clip library. |
| `deploy/` | Docker Compose (dev and production), Caddy (HTTPS), server scripts (ingest, backup, update). |
| `infra/terraform/` | AWS: VM, data volume, S3 bucket, IAM, secrets in SSM. |
| `scripts/vh` | Admin from your computer: publish matches, squads, users, updates. |
| `docs/` | Design notes. |

## Principles

- **Data stays out of this repo.** Scouts, videos, clips, rosters and notes
  live on the server and in backups only (see `.gitignore`).
- **Players are people, not shirt numbers.** Shirt numbers are stored per match.
- **The `.dvw` files are the source of truth**; the database can always be
  rebuilt from them.
- **Clients are interchangeable.** Web, MCP and REST share the same functions
  and the same permissions.

## License

MIT
