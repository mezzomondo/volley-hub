# Local development

Requirements: Docker, R with `datavolley`, `DBI`, `RPostgres`, `dplyr`.

```sh
cp .env.example .env            # then set real passwords
docker compose -f deploy/compose.dev.yml --env-file .env up -d
```

The schema in `db/schema.sql` is applied when the database volume is first created.
To start again from scratch: `docker compose -f deploy/compose.dev.yml down -v`.

## Loading data

1. Create our team and its squad (one CSV per team: `player,aliases,role`, aliases separated by `|`):

   ```sh
   Rscript ingest/load_squad.R players.csv --team "Onyx NVL" --season 2026-27 \
       --dv-names "Onyx,Onyx London" --gender men --league "Men's Division 2 South"
   ```

2. Load matches. Opponent teams and players are created automatically; our players are matched to
   the squad by name or alias, never by shirt number. Re-running a file replaces that match.

   ```sh
   Rscript ingest/ingest_dvw.R match.dvw --team "Onyx NVL"
   ```

The script prints the number of rallies, actions, actions without a player (e.g. `#00` blockers)
and scouting tags (`>NOTE_...` rows).
