-- Adds transparent garment cutouts for the canvas builder.
alter table public.wardrobe_items
    add column if not exists cutout_path text;
