# Shared helpers for the ingest scripts.

`%||%` <- function(a, b) if (is.null(a) || length(a) == 0 || all(is.na(a))) b else a

repo_root <- function() {
  here <- dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))))
  normalizePath(file.path(here, ".."))
}

# Positional arguments plus --key value options.
parse_args <- function(argv, keys) {
  out <- list(positional = character())
  i <- 1
  while (i <= length(argv)) {
    a <- argv[i]
    if (startsWith(a, "--")) {
      k <- sub("^--", "", a)
      if (!k %in% keys) stop("unknown option --", k)
      out[[k]] <- argv[i + 1]
      i <- i + 2
    } else {
      out$positional <- c(out$positional, a)
      i <- i + 1
    }
  }
  out
}

db_connect <- function() {
  env <- file.path(repo_root(), ".env")
  if (file.exists(env)) readRenviron(env)
  DBI::dbConnect(RPostgres::Postgres(),
                 host = Sys.getenv("PGHOST", "127.0.0.1"),
                 port = as.integer(Sys.getenv("PGPORT", "5432")),
                 dbname = Sys.getenv("PGDATABASE", "volleyhub"),
                 user = Sys.getenv("PGUSER", "volleyhub"),
                 password = Sys.getenv("PGPASSWORD"))
}

pg_array <- function(x) {
  x <- as.character(x)
  paste0("{", paste(ifelse(is.na(x), "NULL", paste0('"', gsub('"', '\\\\"', x), '"')), collapse = ","), "}")
}

parse_pg_array <- function(s) {
  if (is.null(s) || is.na(s) || s %in% c("{}", "")) return(character())
  s <- gsub('^\\{|\\}$', "", s)
  gsub('^"|"$', "", strsplit(s, ",")[[1]])
}

sha256_file <- function(path) {
  out <- if (nzchar(Sys.which("sha256sum"))) system2("sha256sum", shQuote(path), stdout = TRUE)
         else system2("shasum", c("-a", "256", shQuote(path)), stdout = TRUE)
  sub(" .*$", "", out[1])
}

clean_name <- function(x) gsub("\\s+", " ", trimws(x))

find_or_create_team <- function(con, dv_name) {
  hit <- DBI::dbGetQuery(con, "
    select id from team
    where lower(name) = lower($1) or lower($1) = any (select lower(n) from unnest(dv_names) n)",
    list(dv_name))
  if (nrow(hit) > 1) stop("team name '", dv_name, "' matches more than one team")
  if (nrow(hit) == 1) return(hit$id)
  DBI::dbGetQuery(con, "insert into team (name, dv_names) values ($1, $2::text[]) returning id",
                  list(dv_name, pg_array(dv_name)))$id
}

# People are matched by full name or alias, case-insensitive. Never by shirt number.
find_or_create_person <- function(con, name) {
  name <- clean_name(name)
  hit <- DBI::dbGetQuery(con, "
    select id from person
    where lower(full_name) = lower($1) or lower($1) = any (select lower(a) from unnest(aliases) a)",
    list(name))
  if (nrow(hit) > 1) stop("name '", name, "' matches more than one person: fix aliases")
  if (nrow(hit) == 1) return(hit$id)
  DBI::dbGetQuery(con, "insert into person (full_name) values ($1) returning id", list(name))$id
}
