#!/usr/bin/env Rscript
# Load one DataVolley .dvw file into the volley-hub database.
#
#   Rscript ingest/ingest_dvw.R <file.dvw> --team "Onyx NVL" [--season 2026-27] [--competition "..."]
#       [--video-key Onyx_Wessex_2026-11-22.mp4]   (file name in the video store; default: the .dvw's video file name)
#
# --team is our team in this match (must exist in the team table, see load_squad.R).
# Re-running on the same match replaces it (match data is rebuilt from the .dvw).
# Connection settings come from the environment or from .env in the repo root
# (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD).

suppressPackageStartupMessages({
  library(datavolley)
  library(DBI)
  library(dplyr)
})

source(file.path(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)))), "common.R"))

args <- parse_args(commandArgs(TRUE), c("team", "season", "competition", "video-key"))
if (length(args$positional) != 1 || is.null(args$team)) {
  stop("usage: ingest_dvw.R <file.dvw> --team <our team> [--season 2026-27] [--competition ...]")
}
dvw <- normalizePath(args$positional[1], mustWork = TRUE)

x <- dv_read(dvw, skill_evaluation_decode = "default")
p <- plays(x)
meta <- x$meta

con <- db_connect()
on.exit(dbDisconnect(con), add = TRUE)

season <- args$season %||% meta$match$season %||% NA_character_
competition <- args$competition %||% meta$match$league %||% NA_character_
played_on <- as.Date(meta$match$date)
if (is.na(played_on)) stop("the .dvw has no match date")

invisible(dbWithTransaction(con, {
  if (!is.na(season)) {
    dbExecute(con, "insert into season (id) values ($1) on conflict do nothing", list(season))
  }

  ours <- dbGetQuery(con, "select id, name, dv_names from team where name = $1 and is_ours", list(args$team))
  if (nrow(ours) != 1) stop("unknown team '", args$team, "': load its squad first (load_squad.R)")

  # Teams in the file: ours is the one whose dvw name matches (or the home team if none does).
  dv_teams <- meta$teams$team
  our_dv_names <- c(ours$name, parse_pg_array(ours$dv_names))
  our_side <- which(tolower(dv_teams) %in% tolower(our_dv_names))
  if (length(our_side) != 1) {
    stop("cannot tell which side is '", args$team, "' (teams in file: ", paste(dv_teams, collapse = ", "),
         "). Add the file's name to team.dv_names.")
  }
  team_ids <- integer(2)
  team_ids[our_side] <- ours$id
  team_ids[3 - our_side] <- find_or_create_team(con, dv_teams[3 - our_side])
  home_id <- team_ids[1]; visiting_id <- team_ids[2]

  # Same match loaded again: keep its id (clips refer to it) and rebuild its contents.
  res <- meta$result
  set_scores <- res$score[!is.na(res$played) & res$played]
  values <- list(season, played_on, competition, home_id, visiting_id,
                 meta$teams$sets_won[1], meta$teams$sets_won[2], pg_array(set_scores),
                 basename(dvw), sha256_file(dvw))
  existing <- dbGetQuery(con, "select id from match where played_on = $1 and home_team_id = $2 and visiting_team_id = $3",
                         list(played_on, home_id, visiting_id))
  if (nrow(existing)) {
    match_id <- existing$id
    for (tbl in c("tag", "action", "rally", "match_player", "video")) {
      dbExecute(con, sprintf("delete from %s where match_id = $1", tbl), list(match_id))
    }
    dbExecute(con, "
      update match set season_id = $1, played_on = $2, competition = $3, home_team_id = $4, visiting_team_id = $5,
             home_sets = $6, visiting_sets = $7, set_scores = $8::text[], source_file = $9, source_sha256 = $10,
             ingested_at = now()
      where id = $11", c(values, list(match_id)))
  } else {
    match_id <- dbGetQuery(con, "
      insert into match (season_id, played_on, competition, home_team_id, visiting_team_id,
                         home_sets, visiting_sets, set_scores, source_file, source_sha256)
      values ($1, $2, $3, $4, $5, $6, $7, $8::text[], $9, $10) returning id", values)$id
  }

  # Players: people by name, shirt numbers per match.
  roster <- bind_rows(
    meta$players_h |> mutate(team_id = home_id),
    meta$players_v |> mutate(team_id = visiting_id)
  )
  roster$person_id <- vapply(seq_len(nrow(roster)), function(i) {
    find_or_create_person(con, roster$name[i])
  }, integer(1))
  dbAppendTable(con, "match_player", data.frame(
    match_id = match_id,
    team_id = roster$team_id,
    person_id = roster$person_id,
    shirt_number = roster$number,
    dv_player_id = roster$player_id,
    dv_role = roster$role,
    is_libero = !is.na(roster$special_role) & grepl("L", roster$special_role)
  ))
  person_of <- function(team_id, number) {
    key <- paste(team_id, number)
    roster$person_id[match(key, paste(roster$team_id, roster$number))]
  }
  team_id_by_name <- function(nm) ifelse(nm == dv_teams[1], home_id, ifelse(nm == dv_teams[2], visiting_id, NA_integer_))

  # Rallies.
  rallies <- p |>
    filter(!is.na(point_id)) |>
    group_by(point_id) |>
    summarise(
      # The set of the first touch: in ovscout2 files the setter-position rows (az3, *z5) that open a set
      # share the point_id of its first rally but still carry the previous set and its final score.
      set_number = { s <- set_number[!is.na(skill)]; if (length(s)) s[1] else last(set_number) },
      serving_team = first(na.omit(serving_team)),
      point_won_by = first(na.omit(point_won_by)),
      home_setter_position = first(home_setter_position),
      visiting_setter_position = first(visiting_setter_position),
      home_lineup = pg_array(c(first(home_p1), first(home_p2), first(home_p3), first(home_p4), first(home_p5), first(home_p6))),
      visiting_lineup = pg_array(c(first(visiting_p1), first(visiting_p2), first(visiting_p3), first(visiting_p4), first(visiting_p5), first(visiting_p6))),
      video_start_s = suppressWarnings(min(video_time, na.rm = TRUE)),
      video_end_s = suppressWarnings(max(video_time, na.rm = TRUE)),
      .groups = "drop"
    ) |>
    mutate(across(c(video_start_s, video_end_s), ~ ifelse(is.finite(.x), .x, NA_real_))) |>
    filter(!is.na(point_won_by)) |>   # drop pseudo-points (timeouts, substitutions, set ends)
    # Score at the start of each rally, counted from who won the rallies before it in the same set.
    arrange(point_id) |>
    group_by(set_number) |>
    mutate(home_score_start = lag(cumsum(point_won_by == dv_teams[1]), default = 0L),
           visiting_score_start = lag(cumsum(point_won_by == dv_teams[2]), default = 0L)) |>
    ungroup()
  # The rally winners must add up to the set scores written in the file; if not, the scout has a
  # point given to the wrong team. Load anyway and say where.
  counted <- rallies |> group_by(set_number) |>
    summarise(score = paste0(sum(point_won_by == dv_teams[1]), "-", sum(point_won_by == dv_teams[2])), .groups = "drop")
  bad <- which(counted$score != set_scores[counted$set_number])
  for (i in bad) message(sprintf("WARNING set %d: rally winners give %s, the file says %s (a point given to the wrong team?)",
                                 counted$set_number[i], counted$score[i], set_scores[counted$set_number[i]]))
  staged <- data.frame(
    match_id = match_id,
    point_id = rallies$point_id,
    set_number = rallies$set_number,
    home_score_start = rallies$home_score_start,
    visiting_score_start = rallies$visiting_score_start,
    serving_team_id = team_id_by_name(rallies$serving_team),
    won_by_team_id = team_id_by_name(rallies$point_won_by),
    home_setter_position = rallies$home_setter_position,
    visiting_setter_position = rallies$visiting_setter_position,
    home_lineup = rallies$home_lineup,
    visiting_lineup = rallies$visiting_lineup,
    video_start_s = rallies$video_start_s,
    video_end_s = rallies$video_end_s
  )
  dbWriteTable(con, "staged_rally", staged, temporary = TRUE, overwrite = TRUE)
  dbExecute(con, "
    insert into rally select match_id, point_id, set_number, home_score_start, visiting_score_start,
      serving_team_id, won_by_team_id, home_setter_position, visiting_setter_position,
      home_lineup::int[], visiting_lineup::int[], video_start_s, video_end_s
    from staged_rally")

  # Actions (touches with a skill).
  skills <- c("Serve", "Reception", "Set", "Attack", "Block", "Dig", "Freeball")
  a <- p |> filter(skill %in% skills, point_id %in% rallies$point_id)
  a_team <- team_id_by_name(a$team)
  dbAppendTable(con, "action", data.frame(
    match_id = match_id,
    point_id = a$point_id,
    seq = a$file_line_number,
    set_number = a$set_number,
    team_id = a_team,
    person_id = person_of(a_team, a$player_number),
    shirt_number = a$player_number,
    skill = a$skill,
    skill_type = a$skill_type,
    evaluation_code = a$evaluation_code,
    evaluation = a$evaluation,
    attack_code = a$attack_code,
    set_code = a$set_code,
    start_zone = a$start_zone,
    end_zone = a$end_zone,
    end_subzone = a$end_subzone,
    end_cone = if ("end_cone" %in% names(a)) a$end_cone else NA_integer_,
    num_players = a$num_players,
    special_code = a$special_code,
    phase = a$phase,
    home_score_start = rallies$home_score_start[match(a$point_id, rallies$point_id)],
    visiting_score_start = rallies$visiting_score_start[match(a$point_id, rallies$point_id)],
    video_time_s = a$video_time,
    code = a$code
  ))

  # Scouting labels.
  notes <- p |> filter(grepl("^>NOTE_", code))
  if (nrow(notes)) {
    dbAppendTable(con, "tag", data.frame(
      match_id = match_id,
      set_number = notes$set_number,
      label = gsub("_", " ", sub("^>NOTE_", "", notes$code)),
      video_time_s = notes$video_time
    ))
  }

  # Video reference (file name only: the video store decides where it lives).
  if (!is.null(args[["video-key"]])) {
    dbAppendTable(con, "video", data.frame(match_id = match_id, camera = "Camera0", storage_key = args[["video-key"]]))
  } else if (!is.null(meta$video) && nrow(meta$video)) {
    dbAppendTable(con, "video", data.frame(
      match_id = match_id,
      camera = meta$video$camera,
      storage_key = basename(meta$video$file)
    ))
  }

  counts <- dbGetQuery(con, "
    select (select count(*) from rally where match_id = $1) as rallies,
           (select count(*) from action where match_id = $1) as actions,
           (select count(*) from action where match_id = $1 and person_id is null) as actions_no_player,
           (select count(*) from tag where match_id = $1) as tags", list(match_id)) |> lapply(as.integer)
  message(sprintf("match %d: %s %s vs %s (%s) | %d rallies, %d actions (%d without player), %d tags",
                  match_id, played_on, dv_teams[1], dv_teams[2], paste(set_scores, collapse = ", "),
                  counts$rallies, counts$actions, counts$actions_no_player, counts$tags))
}))
