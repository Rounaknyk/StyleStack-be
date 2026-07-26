-- Durable daily allowances for AI-backed user actions. Safe to rerun.
create table if not exists public.ai_usage_daily (
    owner_firebase_uid text not null references public.profiles(firebase_uid) on delete cascade,
    operation text not null,
    usage_date date not null default current_date,
    used integer not null default 0 check (used >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (owner_firebase_uid, operation, usage_date)
);
create index if not exists ai_usage_daily_owner_date_idx on public.ai_usage_daily (owner_firebase_uid, usage_date desc);
create or replace function public.consume_ai_daily_quota(
    p_owner_firebase_uid text, p_operation text, p_limit integer, p_units integer default 1
) returns table (allowed boolean, used integer, remaining integer)
language plpgsql security definer set search_path = public as $$
declare v_used integer;
begin
    if p_limit < 1 or p_units < 1 then raise exception 'quota limit and units must be positive'; end if;
    insert into public.ai_usage_daily (owner_firebase_uid, operation, usage_date, used)
    values (p_owner_firebase_uid, p_operation, current_date, p_units)
    on conflict (owner_firebase_uid, operation, usage_date) do update
      set used = public.ai_usage_daily.used + p_units, updated_at = now()
      where public.ai_usage_daily.used + p_units <= p_limit
    returning public.ai_usage_daily.used into v_used;
    if found then return query select true, v_used, greatest(0, p_limit - v_used); return; end if;
    select d.used into v_used from public.ai_usage_daily d where d.owner_firebase_uid = p_owner_firebase_uid and d.operation = p_operation and d.usage_date = current_date;
    return query select false, coalesce(v_used, 0), greatest(0, p_limit - coalesce(v_used, 0));
end;
$$;
alter table public.ai_usage_daily enable row level security;
