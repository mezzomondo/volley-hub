## Report worker: takes queued reports from the database and renders them.
## season / setter_flow: the coach's scripts in $TOOLS_DIR (season_report.R, setter_flow.R, season_lib.R), run on a
## temporary <season>/<team>/ folder rebuilt from the ingested .dvw files (scout/) and the database (stagione/).
## match: volleyreport's match report of one .dvw.
suppressMessages({ library(DBI); library(RPostgres); library(jsonlite) })

tools_dir <- Sys.getenv("TOOLS_DIR", "/data/tools")
scouts_dir <- Sys.getenv("SCOUTS_DIR", "/data/scouts")
report_dir <- Sys.getenv("REPORT_DIR", "/data/reports")
dir.create(report_dir, showWarnings = FALSE, recursive = TRUE)

connect <- function() dbConnect(Postgres(), host = Sys.getenv("PGHOST", "db"), port = as.integer(Sys.getenv("PGPORT", "5432")),
                                dbname = Sys.getenv("PGDATABASE", "volleyhub"), user = Sys.getenv("PGUSER", "volleyhub"),
                                password = Sys.getenv("PGPASSWORD"))
say <- function(...) message(format(Sys.time(), "%Y-%m-%d %H:%M:%S "), ...)

find_scout <- function(name) {
  hits <- list.files(scouts_dir, pattern = paste0("^", gsub("([.()+])", "\\\\\\1", name), "$"), recursive = TRUE, full.names = TRUE)
  if (!length(hits)) stop("scout file not on the server: ", name)
  hits[order(file.mtime(hits), decreasing = TRUE)][1]
}

run_r <- function(args) {
  out <- suppressWarnings(system2("Rscript", args, stdout = TRUE, stderr = TRUE))
  status <- attr(out, "status"); if (is.null(status)) status <- 0L
  list(ok = status == 0L, log = paste(out, collapse = "\n"))
}

render <- function(job) {
  spec <- fromJSON(job$spec, simplifyDataFrame = TRUE)
  ext <- spec$format
  out <- file.path(report_dir, sprintf("%06d.%s", as.integer(job$id), ext))
  root <- tempfile("report-"); on.exit(unlink(root, recursive = TRUE), add = TRUE)
  team_dir <- file.path(root, spec$season, spec$team_dir)
  dir.create(file.path(team_dir, "scout"), recursive = TRUE); dir.create(file.path(team_dir, "stagione"))
  for (f in spec$matches$file) file.copy(find_scout(f), file.path(team_dir, "scout", f))
  utils::write.csv(spec$matches, file.path(team_dir, "stagione", "partite.csv"), row.names = FALSE)
  players <- if (length(spec$players)) as.data.frame(spec$players) else data.frame(player = character(), aliases = character(), role = character())
  utils::write.csv(players, file.path(team_dir, "stagione", "giocatori.csv"), row.names = FALSE)

  if (spec$kind == "season") {
    r <- run_r(c(file.path(tools_dir, "season_report.R"), team_dir))
    made <- list.files(file.path(team_dir, "stagione"), pattern = "_season_.*\\.pdf$", full.names = TRUE)
    if (r$ok && length(made)) file.copy(made[1], out, overwrite = TRUE)
  } else if (spec$kind == "setter_flow") {
    r <- run_r(c(file.path(tools_dir, "setter_flow.R"), team_dir, out))
  } else {
    code <- sprintf('x <- datavolley::dv_read("%s", skill_evaluation_decode = "guess"); volleyreport::vr_match_summary(x, outfile = "%s", format = "paged_pdf")',
                    file.path(team_dir, "scout", spec$matches$file[1]), out)
    r <- run_r(c("-e", shQuote(code)))
  }
  list(ok = r$ok && file.exists(out), log = r$log, key = basename(out))
}

say("report worker started (tools: ", tools_dir, ")")
con <- NULL
repeat {
  tryCatch({
    if (is.null(con) || !dbIsValid(con)) con <- connect()
    dbExecute(con, "update report set status = 'queued' where status = 'rendering' and created_at < now() - interval '30 minutes'")
    job <- dbGetQuery(con, "update report set status = 'rendering'
                            where id = (select id from report where status = 'queued' order by id for update skip locked limit 1)
                            returning id, kind, spec::text as spec")
    if (!nrow(job)) { Sys.sleep(3); next }
    say("rendering report ", job$id, " (", job$kind, ")")
    res <- tryCatch(render(job), error = function(e) list(ok = FALSE, log = conditionMessage(e), key = NA))
    tail_log <- substr(res$log, max(1, nchar(res$log) - 4000), nchar(res$log))
    if (res$ok) {
      dbExecute(con, "update report set status = 'ready', storage_key = $1, log = $2, error = null, finished_at = now() where id = $3",
                list(res$key, tail_log, job$id))
      say("report ", job$id, " ready")
    } else {
      err <- tail(strsplit(res$log, "\n")[[1]], 6)
      dbExecute(con, "update report set status = 'failed', log = $1, error = $2, finished_at = now() where id = $3",
                list(tail_log, paste(err, collapse = "\n"), job$id))
      say("report ", job$id, " failed: ", paste(err, collapse = " | "))
    }
  }, error = function(e) { say("worker error: ", conditionMessage(e)); con <<- NULL; Sys.sleep(5) })
}
