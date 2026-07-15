-- Adds the private optimized thumbnail used by wardrobe grids.
-- Safe to run more than once in the Supabase SQL Editor.

alter table public.wardrobe_items
    add column if not exists thumbnail_path text;
