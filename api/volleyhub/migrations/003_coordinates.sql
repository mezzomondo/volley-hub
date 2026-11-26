-- Court coordinates of each touch (DataVolley grid converted by datavolley: x 0.5-3.5, y 0.5-6.5 is the
-- court, net at y 3.5) and where end_zone came from: the scouted code or, when the code has none, the
-- end coordinates.
alter table action
    add column start_x numeric, add column start_y numeric,
    add column end_x numeric, add column end_y numeric,
    add column end_zone_source text check (end_zone_source in ('code', 'coordinates'));
