-- Live tester subscription/ad bypasses managed from Supabase.
-- Safe to run repeatedly. Store verified Firebase emails in lowercase.

create table if not exists public.access_overrides (
    email text primary key check (email = lower(trim(email))),
    bypass_subscription boolean not null default true,
    bypass_ads boolean not null default true,
    enabled boolean not null default true,
    note text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

drop trigger if exists access_overrides_set_updated_at on public.access_overrides;
create trigger access_overrides_set_updated_at
before update on public.access_overrides
for each row execute function public.set_updated_at();

alter table public.access_overrides enable row level security;
