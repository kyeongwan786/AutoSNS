-- Run this once in Supabase SQL Editor. New signups stay pending until an
-- administrator inserts an active row into customer_licenses.
create table if not exists public.customer_licenses (
  user_id uuid primary key references auth.users(id) on delete cascade,
  status text not null default 'pending'
    check (status in ('pending', 'active', 'suspended')),
  expires_at timestamptz,
  daily_comment_limit integer not null default 100 check (daily_comment_limit >= 0),
  notes text,
  created_at timestamptz not null default now()
);

alter table public.customer_licenses enable row level security;
revoke all on public.customer_licenses from anon;
grant select on public.customer_licenses to authenticated;
drop policy if exists "users read own license" on public.customer_licenses;
create policy "users read own license" on public.customer_licenses
  for select to authenticated using (auth.uid() = user_id);

create table if not exists public.comment_usage (
  user_id uuid not null references auth.users(id) on delete cascade,
  usage_date date not null default (now() at time zone 'utc')::date,
  used integer not null default 0 check (used >= 0),
  primary key (user_id, usage_date)
);
alter table public.comment_usage enable row level security;
revoke all on public.comment_usage from anon, authenticated;

create or replace function public.claim_comment_quota(p_user_id uuid)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  v_limit integer;
  v_used integer;
begin
  if auth.role() <> 'service_role' then
    raise exception 'not authorized';
  end if;
  select daily_comment_limit into v_limit
    from customer_licenses
    where user_id = p_user_id and status = 'active'
      and (expires_at is null or expires_at > now());
  if not found then return false; end if;
  insert into comment_usage (user_id, usage_date, used)
    values (p_user_id, (now() at time zone 'utc')::date, 1)
    on conflict (user_id, usage_date) do update
      set used = comment_usage.used + 1
      where comment_usage.used < v_limit
    returning used into v_used;
  return found;
end;
$$;
revoke all on function public.claim_comment_quota(uuid) from public, anon, authenticated;
grant execute on function public.claim_comment_quota(uuid) to service_role;
