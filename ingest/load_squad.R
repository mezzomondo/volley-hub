#!/usr/bin/env Rscript
# Create or update one of our teams and its squad for a season.
#
#   Rscript ingest/load_squad.R <players.csv> --team "Onyx NVL" --season 2026-27
#       [--dv-names "Onyx,Onyx London"] [--gender men] [--league "Men's Division 2 South"]
#
# players.csv columns: player, aliases (separated by |), role.
# --dv-names: how the team is called inside .dvw files.

suppressPackageStartupMessages(library(DBI))
source(file.path(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)))), "common.R"))

args <- parse_args(commandArgs(TRUE), c("team", "season", "dv-names", "gender", "league"))
if (length(args$positional) != 1 || is.null(args$team) || is.null(args$season)) {
  stop("usage: load_squad.R <players.csv> --team <name> --season <id> [--dv-names a,b] [--gender men|women] [--league ...]")
}
players <- read.csv(args$positional[1], stringsAsFactors = FALSE, na.strings = "")

con <- db_connect()
on.exit(dbDisconnect(con), add = TRUE)

invisible(dbWithTransaction(con, {
  dbExecute(con, "insert into season (id) values ($1) on conflict do nothing", list(args$season))
  dv_names <- if (is.null(args[["dv-names"]])) character() else trimws(strsplit(args[["dv-names"]], ",")[[1]])
  team_id <- dbGetQuery(con, "
    insert into team (name, dv_names, is_ours, gender, league) values ($1, $2::text[], true, $3, $4)
    on conflict (name) do update set
      dv_names = (select array(select distinct unnest(team.dv_names || excluded.dv_names))),
      is_ours = true,
      gender = coalesce(excluded.gender, team.gender),
      league = coalesce(excluded.league, team.league)
    returning id",
    list(args$team, pg_array(dv_names), args$gender %||% NA_character_, args$league %||% NA_character_))$id

  for (i in seq_len(nrow(players))) {
    name <- clean_name(players$player[i])
    aliases <- if (is.na(players$aliases[i])) character() else clean_name(strsplit(players$aliases[i], "\\|")[[1]])
    pid <- find_or_create_person(con, name)
    # A name found only as an alias of someone else would be a different person: keep the canonical name.
    if (length(aliases)) {
      dbExecute(con, "
        update person set aliases = (select array(select distinct unnest(aliases || $2::text[])))
        where id = $1", list(pid, pg_array(aliases)))
    }
    dbExecute(con, "
      insert into squad_member (person_id, team_id, season_id, role) values ($1, $2, $3, $4)
      on conflict (person_id, team_id, season_id) do update set role = excluded.role",
      list(pid, team_id, args$season, players$role[i] %||% NA_character_))
  }
  n <- dbGetQuery(con, "select count(*) as n from squad_member where team_id = $1 and season_id = $2",
                  list(team_id, args$season))$n |> as.integer()
  message(sprintf("%s %s: %d players in squad", args$team, args$season, n))
}))
