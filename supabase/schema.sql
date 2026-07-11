-- StyleStack initial PostgreSQL schema
-- Run this file in the Supabase SQL Editor.

create extension if not exists "pgcrypto";

create table if not exists public.profiles (
    id uuid primary key default gen_random_uuid(),
    firebase_uid text not null unique,
    display_name text,
    email text,
    avatar_url text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.wardrobe_items (
    id uuid primary key default gen_random_uuid(),
    owner_firebase_uid text not null references public.profiles(firebase_uid) on delete cascade,
    name text not null,
    category text not null,
    subcategory text,
    brand text,
    color text,
    size text,
    season text[] not null default '{}',
    tags text[] not null default '{}',
    notes text,
    purchase_date date,
    purchase_price numeric(10, 2) check (purchase_price is null or purchase_price >= 0),
    currency char(3),
    image_path text,
    is_favorite boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.wear_logs (
    id uuid primary key default gen_random_uuid(),
    wardrobe_item_id uuid not null references public.wardrobe_items(id) on delete cascade,
    owner_firebase_uid text not null references public.profiles(firebase_uid) on delete cascade,
    worn_at timestamptz not null default now(),
    notes text,
    created_at timestamptz not null default now()
);

create index if not exists wardrobe_items_owner_idx
    on public.wardrobe_items (owner_firebase_uid);
create index if not exists wardrobe_items_owner_category_idx
    on public.wardrobe_items (owner_firebase_uid, category);
create index if not exists wardrobe_items_tags_idx
    on public.wardrobe_items using gin (tags);
create index if not exists wear_logs_owner_worn_at_idx
    on public.wear_logs (owner_firebase_uid, worn_at desc);
create index if not exists wear_logs_item_worn_at_idx
    on public.wear_logs (wardrobe_item_id, worn_at desc);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists profiles_set_updated_at on public.profiles;
create trigger profiles_set_updated_at
before update on public.profiles
for each row execute function public.set_updated_at();

drop trigger if exists wardrobe_items_set_updated_at on public.wardrobe_items;
create trigger wardrobe_items_set_updated_at
before update on public.wardrobe_items
for each row execute function public.set_updated_at();

-- The FastAPI server uses the Supabase service-role key and enforces access by
-- verified Firebase UID. RLS is enabled to deny direct client access by default.
alter table public.profiles enable row level security;
alter table public.wardrobe_items enable row level security;
alter table public.wear_logs enable row level security;

-- Create the private Storage bucket. Upload objects under <firebase_uid>/<file>.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'wardrobe-images',
    'wardrobe-images',
    false,
    10485760,
    array['image/jpeg', 'image/png', 'image/webp']
)
on conflict (id) do update set
    public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;
