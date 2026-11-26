-- Serve types a player really uses (e.g. {'Jump serve','Jump-float serve'}): scout_check only flags others.
alter table person add column serve_types text[];
