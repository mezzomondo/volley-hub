-- volley-hub schema
-- Source of truth is the .dvw files: everything under "match data" can be
-- rebuilt by re-ingesting them. People, squads, clips and staff notes are
-- managed data and must be backed up.

create extension if not exists pg_trgm;

-- ---------------------------------------------------------------- people & teams

create table season (
    id          text primary key,            -- '2026-27'
    starts_on   date,
    ends_on     date
);

create table team (
    id          serial primary key,
    name        text not null unique,        -- 'Onyx NVL', 'Wessex'
    dv_names    text[] not null default '{}',-- names used in .dvw files ('Onyx')
    is_ours     boolean not null default false,
    gender      text check (gender in ('men', 'women', 'mixed')),
    league      text
);

-- A person is identified by name (plus aliases), never by shirt number.
create table person (
    id          serial primary key,
    full_name   text not null,
    aliases     text[] not null default '{}',
    created_at  timestamptz not null default now()
);
create unique index person_full_name_uq on person (lower(full_name));
create index person_name_trgm on person using gin (full_name gin_trgm_ops);

-- Who plays for which of our teams in a season (opponents only appear in match_player).
create table squad_member (
    person_id   int  not null references person(id),
    team_id     int  not null references team(id),
    season_id   text not null references season(id),
    role        text,                         -- setter, outside, opposite, middle, libero
    active      boolean not null default true,
    primary key (person_id, team_id, season_id)
);

-- ---------------------------------------------------------------- match data

create table match (
    id              serial primary key,
    season_id       text references season(id),
    played_on       date not null,
    competition     text,
    home_team_id    int not null references team(id),
    visiting_team_id int not null references team(id),
    home_sets       int,
    visiting_sets   int,
    set_scores      text[],                   -- {'28-30','15-25','15-25'}
    source_file     text not null,            -- original .dvw file name
    source_sha256   text not null,
    ingested_at     timestamptz not null default now(),
    unique (played_on, home_team_id, visiting_team_id)
);

-- Shirt numbers live here: they can change from match to match.
create table match_player (
    match_id        int  not null references match(id) on delete cascade,
    team_id         int  not null references team(id),
    person_id       int  not null references person(id),
    shirt_number    int  not null,
    dv_player_id    text,                     -- id in the .dvw (e.g. BRI-WES)
    dv_role         text,
    is_libero       boolean not null default false,
    primary key (match_id, team_id, shirt_number)
);

create table video (
    id              serial primary key,
    match_id        int not null references match(id) on delete cascade,
    camera          text not null default 'Camera0',
    storage_key     text,                     -- path/key in the video store
    duration_s      numeric,
    offset_s        numeric not null default 0, -- added to dvw video_time
    unique (match_id, camera)
);

create table rally (
    match_id                int not null references match(id) on delete cascade,
    point_id                int not null,
    set_number              int not null,
    home_score_start        int,
    visiting_score_start    int,
    serving_team_id         int references team(id),
    won_by_team_id          int references team(id),
    home_setter_position    int,
    visiting_setter_position int,
    home_lineup             int[],            -- shirt numbers in zones 1..6
    visiting_lineup         int[],
    video_start_s           numeric,
    video_end_s             numeric,
    primary key (match_id, point_id)
);

create table action (
    id              bigserial primary key,
    match_id        int not null references match(id) on delete cascade,
    point_id        int not null,
    seq             int not null,             -- order within the match (file line)
    set_number      int not null,
    team_id         int not null references team(id),
    person_id       int references person(id),
    shirt_number    int,
    skill           text not null,            -- Serve, Reception, Set, Attack, Block, Dig, Freeball
    skill_type      text,
    evaluation_code text,                     -- # + ! - / =
    evaluation      text,
    attack_code     text,
    set_code        text,
    start_zone      int,
    end_zone        int,
    end_subzone     text,
    num_players     text,
    special_code    text,
    phase           text,                     -- Serve, Reception, Transition
    home_score_start int,
    visiting_score_start int,
    video_time_s    numeric,
    code            text not null,            -- raw DataVolley code
    foreign key (match_id, point_id) references rally(match_id, point_id) on delete cascade
);
create index action_lookup on action (person_id, skill, evaluation_code);
create index action_match on action (match_id, seq);

-- Free-text labels written while scouting (>NOTE_... rows).
create table tag (
    id              bigserial primary key,
    match_id        int not null references match(id) on delete cascade,
    set_number      int,
    label           text not null,
    video_time_s    numeric
);

-- ---------------------------------------------------------------- managed data

create table clip (
    id              bigserial primary key,
    params_hash     text not null unique,     -- same request => same clip, no re-render
    title           text,
    caption         text,
    spec            jsonb not null,           -- {segments:[{match_id,start_s,end_s,slowmo:[a,b]}], ...}
    status          text not null default 'queued'
                    check (status in ('queued', 'rendering', 'ready', 'failed')),
    storage_key     text,
    duration_s      numeric,
    created_by      text,
    created_at      timestamptz not null default now()
);
create table clip_action (
    clip_id         bigint not null references clip(id) on delete cascade,
    action_id       bigint not null references action(id) on delete cascade,
    primary key (clip_id, action_id)
);

-- Coach notes on players: staff only (enforced with row-level security later).
create table staff_note (
    id              bigserial primary key,
    person_id       int  not null references person(id),
    team_id         int  references team(id),
    noted_on        date not null default current_date,
    body            text not null,
    created_by      text
);
