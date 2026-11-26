# Deploy

One VM (EC2, Ubuntu 24.04) running Docker Compose: Postgres, API + MCP, clip worker, Caddy (HTTPS).
State lives on a separate EBS volume mounted at `/data`; videos, scouts, originals and nightly
backups go to an S3 bucket. No SSH: admin goes through AWS Systems Manager.

## First time

```sh
scripts/bootstrap-state.sh            # once: private S3 bucket for the Terraform state, writes backend.hcl
cd infra/terraform
terraform init -backend-config=backend.hcl
terraform plan -out tfplan
terraform apply tfplan
```

The first boot takes about 10 minutes (packages, image builds, certificate). Follow it with
`scripts/vh run 'tail -n 30 /var/log/volley-hub-bootstrap.log'`. Then `terraform output url` should answer.

Without a `domain` variable the site is `https://<ip-with-dashes>.sslip.io` (a public wildcard DNS
that maps the name to the IP, so Let's Encrypt works without any DNS setup). To use your own name, point an
A record to `terraform output public_ip`, set `domain` and update `DOMAIN` in `/opt/volley-hub/.env.prod`
on the server (`vh shell`), then `vh update`.

The Terraform state lives in S3 (versioned, locked with a lock file): it contains the generated
secrets, so never copy it into the repo. The data volume has `prevent_destroy`: `terraform destroy` refuses to delete it.

## Day to day (from your computer)

`scripts/vh` reads the bucket and instance from the Terraform outputs and uses the AWS profile
`volley-hub` (override with `VH_AWS_PROFILE`).

```sh
# our team and its squad (CSV: player,aliases,role)
scripts/vh squad --team "Onyx NVL" --season 2026-27 --dv-names "Onyx,Onyx London" \
    --gender men --league "Men's Division 2 South" giocatori.csv

# a match: scout + video (transcoded to 720p H.264 on your Mac, then uploaded)
scripts/vh publish --team "Onyx NVL" --dvw 2026-11-22_Onyx_Wessex.dvw --video Onyx_Wessex_2026-11-22.MOV

# people
scripts/vh user create-user --name "Casey Hudson" --role player --team "Onyx NVL"
scripts/vh user create-user --name "Club Chair" --role management
scripts/vh user list-users
scripts/vh user revoke --name "Casey Hudson"

scripts/vh update     # deploy the latest commit
scripts/vh backup     # backup now (it also runs every night at 03:30 UTC)
scripts/vh shell      # shell on the server
```

Roles: `player` sees only their teams' matches; `management` sees everything; `staff` also gets the
read-only `query` tool (SQL) and staff notes.

## Connecting an AI assistant (MCP)

Endpoint: `https://<host>/mcp`, header `Authorization: Bearer <token>`.

Clients that accept a bearer header work today, for example:

- Claude Code: `claude mcp add --transport http volley-hub https://<host>/mcp --header "Authorization: Bearer <token>"`
- Cursor / VS Code / Windsurf / Gemini CLI: add an HTTP MCP server with the same URL and header.
- Open WebUI, LibreChat (any model, including local ones): add an MCP (streamable HTTP) tool server with the URL and header.

Web apps that only offer OAuth sign-in for connectors (claude.ai, ChatGPT) need the OAuth server,
which is the next step on the roadmap.

Without MCP: the same functions are a REST API, described by `https://<host>/openapi.json`.
There are no web pages: clips and match videos are signed links to mp4 files (valid 7 days).

## Schema changes

`db/schema.sql` runs only when the database is created. Changes after that need a migration
(planned); until then apply them by hand with `vh run`.
