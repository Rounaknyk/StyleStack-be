-- Canvas Style Builder persistence.
-- Safe to run more than once in Supabase SQL Editor.

create table if not exists public.canvas_styles (
    id uuid primary key default gen_random_uuid(),
    owner_firebase_uid text not null references public.profiles(firebase_uid) on delete cascade,
    name text not null,
    preview_path text,
    items jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists canvas_styles_owner_created_idx
    on public.canvas_styles (owner_firebase_uid, created_at desc);

drop trigger if exists canvas_styles_set_updated_at on public.canvas_styles;
create trigger canvas_styles_set_updated_at
before update on public.canvas_styles
for each row execute function public.set_updated_at();

alter table public.canvas_styles enable row level security;
