-- Roles become admin | staff | player. The old top role (staff) becomes admin; management becomes staff.
alter table app_user drop constraint if exists app_user_role_check;
update app_user set role = case role when 'staff' then 'admin' when 'management' then 'staff' else role end;
alter table app_user add constraint app_user_role_check check (role in ('player', 'staff', 'admin'));
