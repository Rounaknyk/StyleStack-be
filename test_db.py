import asyncio
from app.core.supabase import get_supabase_client

client = get_supabase_client()
print(client.table('app_config').select('*').execute())
