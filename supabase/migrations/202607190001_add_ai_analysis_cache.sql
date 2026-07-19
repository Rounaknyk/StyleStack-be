-- Shared perceptual-image cache for wardrobe AI analysis.
-- Safe to run more than once in the Supabase SQL Editor.

create table if not exists public.ai_image_analysis_cache (
    image_hash text not null,
    analysis_kind text not null
        check (analysis_kind in ('single', 'multiple')),
    analysis jsonb not null,
    provider text,
    hit_count bigint not null default 0,
    created_at timestamptz not null default now(),
    last_used_at timestamptz not null default now(),
    primary key (image_hash, analysis_kind)
);

create index if not exists ai_image_analysis_cache_last_used_idx
    on public.ai_image_analysis_cache (last_used_at desc);

alter table public.ai_image_analysis_cache enable row level security;
