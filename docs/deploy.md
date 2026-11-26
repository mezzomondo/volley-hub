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

Reports (asked from any AI with `create_report`) run the coach's R scripts on the server. The scripts are not in
git: upload them once, and again whenever they change (reports made with older scripts are redone on request):

```sh
scripts/vh tools ~/path/to/strumenti     # season_lib.R, season_report.R, setter_flow.R
```

Other admin commands:

```sh
scripts/vh admin scout-check --match-id 2
scripts/vh admin set-competition --match-id 2 --competition "NVL Division 2"   # kept across reloads
scripts/vh admin set-serve-types --name "Will Zapata" --types "Jump serve,Jump-float serve"
```

Roles: `player` sees only their teams' matches; `management` sees everything; `staff` also gets the
read-only `query` tool (SQL) and staff notes.

## Connecting an AI assistant (MCP)

Everyone gets a personal **access code** from `vh user create-user` (send it privately). With it:

**Apps with connectors (Claude desktop, web and mobile, ChatGPT and other OAuth clients)**

1. Add a custom connector (in Claude: Settings, Connectors, Add custom connector) with the URL
   `https://<host>/mcp`. Nothing else to fill in.
2. Press Connect: a sign-in page opens. Paste the access code once.
3. Done. The app stays connected (it renews its own tokens for up to 90 days of inactivity).

Revoking someone (`vh user revoke --name ...`) or issuing a new code (`vh user rotate-token --name ...`)
signs out every app they connected.

**Developer tools that accept a header (Claude Code, Cursor, VS Code, Gemini CLI, LibreChat...)**
can also use the code directly as a bearer token:

```sh
claude mcp add --transport http volley-hub https://<host>/mcp --header "Authorization: Bearer <access code>"
```

**Scripts:** the same functions are a REST API (`https://<host>/api/...`, schema at
`https://<host>/openapi.json`), with the same header.

There are no web pages apart from the sign-in form: clips and match videos are signed links to mp4
files (valid 7 days).

### How sign-in works

volley-hub is its own OAuth 2.1 authorization server, as the MCP specification describes: protected
resource metadata (RFC 9728) at `/.well-known/oauth-protected-resource/mcp`, server metadata
(RFC 8414), dynamic client registration (RFC 7591), authorization code with PKCE (S256), rotating
refresh tokens and revocation (RFC 7009). Access tokens last one hour. Codes and tokens are stored
only as SHA-256 hashes. Redirect URIs must be https, a loopback http address or an app scheme.

## Schema changes

`db/schema.sql` is the baseline and runs only when the database is created. Later changes are SQL
files in `api/volleyhub/migrations/` (`002_something.sql`, ...): the API applies the new ones at start,
once, in order, and records them in `schema_migration`. `vh update` is enough to roll them out.
