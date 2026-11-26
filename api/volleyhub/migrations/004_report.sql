-- Reports (season, match, setter flow) rendered by the R worker from the ingested .dvw files.
create table report (
    id          bigserial primary key,
    kind        text not null check (kind in ('season', 'match', 'setter_flow')),
    team_id     int references team(id),
    season      text,
    match_id    int references match(id) on delete cascade,
    format      text not null check (format in ('pdf', 'png')),
    params_hash text not null unique,     -- request + the matches' sha256 + the report scripts' sha256
    spec        jsonb not null,           -- everything the worker needs (files, matches, players)
    status      text not null default 'queued' check (status in ('queued', 'rendering', 'ready', 'failed')),
    storage_key text,
    error       text,
    log         text,
    created_by  text,
    created_at  timestamptz not null default now(),
    finished_at timestamptz
);
create index report_status on report (status);
