-- OAuth 2.1 for MCP connectors (claude.ai, Claude Desktop/mobile, ChatGPT...).
-- Tokens and codes are stored as sha256 hashes only. Not readable by volleyhub_reader.

create table oauth_client (
    client_id   text primary key,
    info        jsonb not null,             -- RFC 7591 client information as registered
    created_at  timestamptz not null default now()
);

-- A login in progress: created by /authorize, consumed by the login form.
create table oauth_pending (
    id_hash     text primary key,
    client_id   text not null references oauth_client on delete cascade,
    params      jsonb not null,             -- AuthorizationParams
    expires_at  timestamptz not null
);

create table oauth_code (
    code_hash   text primary key,
    client_id   text not null references oauth_client on delete cascade,
    user_id     int  not null references app_user on delete cascade,
    params      jsonb not null,
    expires_at  timestamptz not null
);

-- Access and refresh tokens. A family is one sign-in: refreshing rotates both, revoking kills the family.
create table oauth_token (
    token_hash  text primary key,
    kind        text not null check (kind in ('access', 'refresh')),
    family      text not null,
    client_id   text not null references oauth_client on delete cascade,
    user_id     int  not null references app_user on delete cascade,
    scopes      text[] not null default '{}',
    resource    text,
    expires_at  timestamptz not null,
    created_at  timestamptz not null default now()
);
create index oauth_token_family on oauth_token (family);
create index oauth_token_user on oauth_token (user_id);
