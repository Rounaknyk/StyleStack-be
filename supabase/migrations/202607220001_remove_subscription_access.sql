-- Removes the deferred subscription bypass while retaining tester ad bypasses.
-- Safe to run more than once after 202607210001_add_access_overrides.sql.

alter table public.access_overrides
    drop column if exists bypass_subscription;
