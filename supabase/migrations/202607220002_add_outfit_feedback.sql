-- Durable implicit and explicit feedback for the hybrid stylist engine.
-- Safe to run more than once in the Supabase SQL Editor.

create table if not exists public.outfit_feedback (
    id uuid primary key default gen_random_uuid(),
    owner_firebase_uid text not null references public.profiles(firebase_uid) on delete cascade,
    outfit_id uuid not null references public.outfits(id) on delete cascade,
    signal text not null check (
        signal in ('worn', 'liked', 'refreshed', 'wore_something_else', 'disliked')
    ),
    reason text,
    created_at timestamptz not null default now(),
    unique (owner_firebase_uid, outfit_id, signal)
);

create index if not exists outfit_feedback_owner_created_idx
    on public.outfit_feedback (owner_firebase_uid, created_at desc);

alter table public.outfit_feedback enable row level security;
