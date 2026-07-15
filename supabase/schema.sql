-- StyleStack initial PostgreSQL schema
-- Run this file in the Supabase SQL Editor.

create extension if not exists "pgcrypto";

create table if not exists public.profiles (
    id uuid primary key default gen_random_uuid(),
    firebase_uid text not null unique,
    display_name text,
    email text,
    avatar_url text,
    city text,
    timezone text not null default 'Asia/Kolkata',
    notification_enabled boolean not null default false,
    notification_time time not null default '08:00',
    last_notification_date date,
    google_calendar_connected boolean not null default false,
    google_calendar_refresh_token text,
    google_calendar_email text,
    google_calendar_last_synced_at timestamptz,
    gender_identity text,
    date_of_birth date,
    body_type text,
    height_cm smallint,
    style_preferences text[] not null default '{}',
    shopping_frequency text,
    onboarding_goals text[] not null default '{}',
    onboarding_completed boolean not null default false,
    onboarding_completed_at timestamptz,
    onboarding_version smallint not null default 1,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.profiles
    add column if not exists city text,
    add column if not exists timezone text not null default 'Asia/Kolkata',
    add column if not exists notification_enabled boolean not null default false,
    add column if not exists notification_time time not null default '08:00',
    add column if not exists last_notification_date date;

alter table public.profiles
    add column if not exists google_calendar_connected boolean not null default false,
    add column if not exists google_calendar_refresh_token text,
    add column if not exists google_calendar_email text,
    add column if not exists google_calendar_last_synced_at timestamptz;

-- Upgrade existing profiles with onboarding fields. Existing rows intentionally
-- remain incomplete so they can receive the new personalization flow.
alter table public.profiles
    add column if not exists gender_identity text,
    add column if not exists date_of_birth date,
    add column if not exists body_type text,
    add column if not exists height_cm smallint,
    add column if not exists style_preferences text[] not null default '{}',
    add column if not exists shopping_frequency text,
    add column if not exists onboarding_goals text[] not null default '{}',
    add column if not exists onboarding_completed boolean not null default false,
    add column if not exists onboarding_completed_at timestamptz,
    add column if not exists onboarding_version smallint not null default 1;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'profiles_gender_identity_check'
          and conrelid = 'public.profiles'::regclass
    ) then
        alter table public.profiles add constraint profiles_gender_identity_check
            check (gender_identity is null or gender_identity in (
                'woman', 'man', 'non_binary', 'prefer_not_to_say'
            ));
    end if;
    if not exists (
        select 1 from pg_constraint
        where conname = 'profiles_date_of_birth_check'
          and conrelid = 'public.profiles'::regclass
    ) then
        alter table public.profiles add constraint profiles_date_of_birth_check
            check (date_of_birth is null or date_of_birth >= date '1900-01-01');
    end if;
    if not exists (
        select 1 from pg_constraint
        where conname = 'profiles_body_type_check'
          and conrelid = 'public.profiles'::regclass
    ) then
        alter table public.profiles add constraint profiles_body_type_check
            check (body_type is null or body_type in (
                'slim', 'average', 'athletic', 'curvy', 'plus', 'not_sure'
            ));
    end if;
    if not exists (
        select 1 from pg_constraint
        where conname = 'profiles_height_cm_check'
          and conrelid = 'public.profiles'::regclass
    ) then
        alter table public.profiles add constraint profiles_height_cm_check
            check (height_cm is null or height_cm between 90 and 230);
    end if;
    if not exists (
        select 1 from pg_constraint
        where conname = 'profiles_style_preferences_check'
          and conrelid = 'public.profiles'::regclass
    ) then
        alter table public.profiles add constraint profiles_style_preferences_check
            check (
                style_preferences <@ array[
                    'formal', 'office', 'casual', 'sporty', 'trendy', 'ethnic',
                    'minimal', 'bohemian', 'glam', 'not_sure', 'explore'
                ]::text[]
                and cardinality(style_preferences) <= 11
            );
    end if;
    if not exists (
        select 1 from pg_constraint
        where conname = 'profiles_shopping_frequency_check'
          and conrelid = 'public.profiles'::regclass
    ) then
        alter table public.profiles add constraint profiles_shopping_frequency_check
            check (shopping_frequency is null or shopping_frequency in (
                'every_week', 'every_month', 'every_2_3_months', 'every_season', 'rarely'
            ));
    end if;
    if not exists (
        select 1 from pg_constraint
        where conname = 'profiles_onboarding_goals_check'
          and conrelid = 'public.profiles'::regclass
    ) then
        alter table public.profiles add constraint profiles_onboarding_goals_check
            check (
                onboarding_goals <@ array[
                    'daily_outfit_ideas', 'organize_wardrobe',
                    'discover_personal_style', 'reduce_decision_fatigue',
                    'shop_less_style_better', 'outfit_inspiration',
                    'track_what_i_wear'
                ]::text[]
                and cardinality(onboarding_goals) <= 7
            );
    end if;
    if not exists (
        select 1 from pg_constraint
        where conname = 'profiles_onboarding_version_check'
          and conrelid = 'public.profiles'::regclass
    ) then
        alter table public.profiles add constraint profiles_onboarding_version_check
            check (onboarding_version > 0);
    end if;
    if not exists (
        select 1 from pg_constraint
        where conname = 'profiles_onboarding_completion_check'
          and conrelid = 'public.profiles'::regclass
    ) then
        alter table public.profiles add constraint profiles_onboarding_completion_check
            check (
                (onboarding_completed and onboarding_completed_at is not null)
                or (not onboarding_completed and onboarding_completed_at is null)
            );
    end if;
end;
$$;

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
    description text,
    formality text,
    notes text,
    purchase_date date,
    purchase_price numeric(10, 2) check (purchase_price is null or purchase_price >= 0),
    currency char(3),
    image_path text,
    thumbnail_path text,
    is_favorite boolean not null default false,
    tagged boolean not null default false,
    ai_tag_status text not null default 'pending'
        check (ai_tag_status in ('pending', 'processing', 'completed', 'failed')),
    ai_category text,
    ai_color text,
    ai_season text,
    ai_formality text,
    ai_description text,
    ai_visual_tags text[] not null default '{}',
    import_source text,
    source_external_id text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Upgrade existing StyleStack databases created before AI tagging was added.
alter table public.wardrobe_items
    add column if not exists description text,
    add column if not exists formality text,
    add column if not exists tagged boolean not null default false,
    add column if not exists ai_tag_status text not null default 'pending',
    add column if not exists ai_category text,
    add column if not exists ai_color text,
    add column if not exists ai_season text,
    add column if not exists ai_formality text,
    add column if not exists ai_description text;

alter table public.wardrobe_items
    add column if not exists thumbnail_path text;

alter table public.wardrobe_items
    add column if not exists ai_visual_tags text[] not null default '{}';

alter table public.wardrobe_items
    add column if not exists import_source text,
    add column if not exists source_external_id text;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'wardrobe_items_ai_tag_status_check'
          and conrelid = 'public.wardrobe_items'::regclass
    ) then
        alter table public.wardrobe_items
            add constraint wardrobe_items_ai_tag_status_check
            check (ai_tag_status in ('pending', 'processing', 'completed', 'failed'));
    end if;
end;
$$;

create table if not exists public.wear_logs (
    id uuid primary key default gen_random_uuid(),
    wardrobe_item_id uuid not null references public.wardrobe_items(id) on delete cascade,
    owner_firebase_uid text not null references public.profiles(firebase_uid) on delete cascade,
    worn_at timestamptz not null default now(),
    notes text,
    created_at timestamptz not null default now()
);

create table if not exists public.outfits (
    id uuid primary key default gen_random_uuid(),
    owner_firebase_uid text not null references public.profiles(firebase_uid) on delete cascade,
    occasion text not null default 'daily',
    reasoning text not null,
    weather jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table if not exists public.outfit_items (
    outfit_id uuid not null references public.outfits(id) on delete cascade,
    wardrobe_item_id uuid not null references public.wardrobe_items(id) on delete cascade,
    position integer not null default 0,
    primary key (outfit_id, wardrobe_item_id)
);

create table if not exists public.canvas_styles (
    id uuid primary key default gen_random_uuid(),
    owner_firebase_uid text not null references public.profiles(firebase_uid) on delete cascade,
    name text not null,
    preview_path text,
    items jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.device_tokens (
    id uuid primary key default gen_random_uuid(),
    owner_firebase_uid text not null references public.profiles(firebase_uid) on delete cascade,
    token text not null unique,
    platform text not null default 'unknown',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.calendar_events (
    id uuid primary key default gen_random_uuid(),
    owner_firebase_uid text not null references public.profiles(firebase_uid) on delete cascade,
    source text not null default 'manual' check (source in ('manual', 'google')),
    external_id text,
    title text not null,
    description text,
    location text,
    start_at timestamptz not null,
    end_at timestamptz,
    all_day boolean not null default false,
    occasion text not null default 'event',
    outfit_id uuid references public.outfits(id) on delete set null,
    reminder_sent_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (owner_firebase_uid, source, external_id)
);

create table if not exists public.app_notifications (
    id uuid primary key default gen_random_uuid(),
    owner_firebase_uid text not null references public.profiles(firebase_uid) on delete cascade,
    type text not null,
    title text not null,
    body text not null,
    data jsonb not null default '{}'::jsonb,
    dedupe_key text,
    read_at timestamptz,
    created_at timestamptz not null default now(),
    unique (owner_firebase_uid, dedupe_key)
);

create table if not exists public.outfit_selfies (
    id uuid primary key default gen_random_uuid(),
    owner_firebase_uid text not null references public.profiles(firebase_uid) on delete cascade,
    image_path text not null,
    status text not null default 'reviewing'
        check (status in ('reviewing', 'confirmed')),
    quality_score numeric(4, 3) not null check (quality_score between 0 and 1),
    quality_feedback text,
    captured_at timestamptz not null default now(),
    confirmed_at timestamptz,
    created_at timestamptz not null default now()
);

create table if not exists public.outfit_selfie_detections (
    id uuid primary key default gen_random_uuid(),
    outfit_selfie_id uuid not null references public.outfit_selfies(id) on delete cascade,
    wardrobe_item_id uuid references public.wardrobe_items(id) on delete set null,
    detected_name text not null,
    detected_category text,
    detected_color text,
    detected_description text,
    visual_tags text[] not null default '{}',
    confidence numeric(4, 3) not null check (confidence between 0 and 1),
    selected boolean not null default true,
    created_at timestamptz not null default now()
);

create index if not exists wardrobe_items_owner_idx
    on public.wardrobe_items (owner_firebase_uid);
create index if not exists wardrobe_items_owner_category_idx
    on public.wardrobe_items (owner_firebase_uid, category);
create index if not exists wardrobe_items_tags_idx
    on public.wardrobe_items using gin (tags);
create index if not exists wardrobe_items_import_idx
    on public.wardrobe_items (owner_firebase_uid, import_source, source_external_id);
create index if not exists wear_logs_owner_worn_at_idx
    on public.wear_logs (owner_firebase_uid, worn_at desc);
create index if not exists wear_logs_item_worn_at_idx
    on public.wear_logs (wardrobe_item_id, worn_at desc);
create index if not exists outfits_owner_created_idx
    on public.outfits (owner_firebase_uid, created_at desc);
create index if not exists canvas_styles_owner_created_idx
    on public.canvas_styles (owner_firebase_uid, created_at desc);
create index if not exists device_tokens_owner_idx
    on public.device_tokens (owner_firebase_uid);
create index if not exists calendar_events_owner_start_idx
    on public.calendar_events (owner_firebase_uid, start_at);
create index if not exists app_notifications_owner_created_idx
    on public.app_notifications (owner_firebase_uid, created_at desc);
create index if not exists outfit_selfies_owner_captured_idx
    on public.outfit_selfies (owner_firebase_uid, captured_at desc);
create index if not exists outfit_selfie_detections_selfie_idx
    on public.outfit_selfie_detections (outfit_selfie_id);

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

drop trigger if exists calendar_events_set_updated_at on public.calendar_events;
create trigger calendar_events_set_updated_at
before update on public.calendar_events
for each row execute function public.set_updated_at();

drop trigger if exists canvas_styles_set_updated_at on public.canvas_styles;
create trigger canvas_styles_set_updated_at
before update on public.canvas_styles
for each row execute function public.set_updated_at();

-- The FastAPI server uses the Supabase service-role key and enforces access by
-- verified Firebase UID. RLS is enabled to deny direct client access by default.
alter table public.profiles enable row level security;
alter table public.wardrobe_items enable row level security;
alter table public.wear_logs enable row level security;
alter table public.outfits enable row level security;
alter table public.outfit_items enable row level security;
alter table public.canvas_styles enable row level security;
alter table public.device_tokens enable row level security;
alter table public.calendar_events enable row level security;
alter table public.app_notifications enable row level security;
alter table public.outfit_selfies enable row level security;
alter table public.outfit_selfie_detections enable row level security;

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
