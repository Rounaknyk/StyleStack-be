create table app_config (
    config_key text primary key,
    config_value jsonb not null,
    updated_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- Enable RLS
alter table app_config enable row level security;

-- Public can read
create policy "Public can read app_config"
    on app_config for select
    using (true);

-- No public write access (enforced by backend service role key)

-- Insert default value
insert into app_config (config_key, config_value) values ('phone_auth_enabled', 'true'::jsonb);
