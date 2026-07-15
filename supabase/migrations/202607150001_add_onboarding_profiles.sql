-- Add StyleStack's personalized onboarding fields to an existing database.
-- Safe to run more than once in the Supabase SQL Editor.

begin;

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
                'every_week', 'every_month', 'every_2_3_months',
                'every_season', 'rarely'
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

commit;
