-- Drill library and training sessions (staff only). Imported per season from a Markdown vault
-- (scripts/library-bundle.py); a new import of a season replaces that season's sessions.
create table library_drill (
    id          serial primary key,
    slug        text not null unique,
    name        text not null,
    theme       text,
    updated_at  timestamptz not null default now()
);

create table library_session (
    id          serial primary key,
    key         text not null unique,          -- <season>/<path in the vault without .md>
    season      text not null,
    grp         text not null,                 -- training day: Wednesday, Thursday, ...
    date        date not null,
    title       text not null,
    kind        text not null default 'session',  -- session | feedback
    players     int,                           -- head count only, no attendance
    intro_md    text,
    pdf_path    text,
    imported_at timestamptz not null default now()
);
create index on library_session (season, date);

create table library_section (
    id          serial primary key,
    session_id  int not null references library_session(id) on delete cascade,
    ord         int not null,
    heading     text not null,
    body_md     text not null default '',
    drill_id    int references library_drill(id),
    duration    text,
    summary     text,
    images      jsonb not null default '[]',   -- [{path, caption}]
    search      tsvector generated always as (
                    to_tsvector('english', heading || ' ' || coalesce(summary, '') || ' ' || body_md)) stored,
    unique (session_id, ord)
);
create index on library_section (drill_id);
create index on library_section using gin (search);

-- The staff "query" tool may read the library too.
grant select on library_drill, library_session, library_section to volleyhub_reader;
