# StyleStack API

FastAPI backend foundation using Firebase Authentication and Supabase PostgreSQL/Storage.

## Project structure

```text
StyleStack-be/
├── app/
│   ├── api/
│   │   ├── routes/users.py       # Protected endpoint
│   │   └── router.py
│   ├── core/
│   │   ├── config.py             # Environment configuration
│   │   ├── firebase.py           # Firebase Admin initialization
│   │   └── supabase.py           # Supabase server client
│   ├── dependencies/auth.py      # Bearer-token auth dependency
│   └── main.py                   # FastAPI app and health endpoint
├── supabase/schema.sql           # Tables, indexes, triggers, RLS, bucket
├── .env.example
├── Dockerfile
├── render.yaml
└── requirements.txt
```

## 1. Firebase setup

1. Create/select a Firebase project and enable the sign-in providers you want in **Authentication > Sign-in method**.
2. In **Project settings > Service accounts**, generate a new private key.
3. Convert the downloaded JSON to one line, then put the entire JSON value in `FIREBASE_SERVICE_ACCOUNT_JSON`. Never commit this credential.
4. Your frontend signs users in with the Firebase client SDK and sends the Firebase **ID token** on API requests:

```http
Authorization: Bearer <firebase-id-token>
```

Do not send the Firebase refresh token or the service-account key to this API.

## 2. Supabase setup

1. Create a Supabase project.
2. Open **SQL Editor**, paste [`supabase/schema.sql`](supabase/schema.sql), and run it. This creates the profile, wardrobe item, and wear-log tables plus the private `wardrobe-images` bucket. The script is safe to rerun when applying these additions to an existing project.
3. Copy the project URL and `service_role` key from Supabase project settings into `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`.

The service-role key bypasses Row Level Security, so it must exist only in this backend. The schema enables RLS without client policies, denying direct browser/mobile access by default. Backend routes should always scope queries by the verified Firebase `uid`, for example:

```python
supabase.table("wardrobe_items").select("*").eq(
    "owner_firebase_uid", current_user["uid"]
).execute()
```

Store image objects using `<firebase_uid>/<generated-filename>` paths and keep `image_path` (not a permanent signed URL) in `wardrobe_items`.

## 3. Run locally

Python 3.11 or 3.12 is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Replace every placeholder in `.env`, then run:

```bash
uvicorn app.main:app --reload
```

Open `http://localhost:8000/docs` for Swagger UI.

### Endpoints

Health check (public):

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"status":"ok","service":"StyleStack API"}
```

Current Firebase user (protected):

```bash
curl http://localhost:8000/api/v1/users/me \
  -H "Authorization: Bearer YOUR_FIREBASE_ID_TOKEN"
```

Expected response:

```json
{"user_id":"firebase-user-uid"}
```

### Wardrobe API

Every wardrobe endpoint requires a Firebase ID token in the `Authorization` header. The API always combines the requested item ID with the verified Firebase UID, so another user's item is returned as `404` rather than exposing its existence.

Upload an image and create an item:

```bash
curl -X POST http://localhost:8000/api/v1/wardrobe/items \
  -H "Authorization: Bearer YOUR_FIREBASE_ID_TOKEN" \
  -F "name=Black blazer" \
  -F "category=Outerwear" \
  -F "brand=Example Brand" \
  -F "color=Black" \
  -F "season=fall,winter" \
  -F "tags=formal,work" \
  -F "image=@/path/to/blazer.jpg"
```

Supported images are JPEG, PNG, and WebP up to 10 MB. Additional optional form fields are `subcategory`, `size`, `notes`, `purchase_date`, `purchase_price`, `currency`, and `is_favorite`.

List and filter items:

```bash
curl "http://localhost:8000/api/v1/wardrobe/items?category=Outerwear&color=Black&tag=formal&is_favorite=true&limit=50&offset=0" \
  -H "Authorization: Bearer YOUR_FIREBASE_ID_TOKEN"
```

Available filters are `category`, `brand`, `color`, `tag`, `is_favorite`, and `search`. Pagination uses `limit` (maximum 100) and `offset`.

Other item operations:

```text
GET    /api/v1/wardrobe/items/{id}
PUT    /api/v1/wardrobe/items/{id}
DELETE /api/v1/wardrobe/items/{id}
POST   /api/v1/wardrobe/items/{id}/wear
```

The update endpoint accepts a JSON object containing any editable fields. Wear logging accepts optional JSON such as:

```json
{
  "worn_at": "2026-07-11T10:00:00Z",
  "notes": "Office event"
}
```

## 4. Deploy to Render

The repository includes `render.yaml`, so it can be deployed as a Render Blueprint:

1. Push this directory to a Git repository.
2. In Render, choose **New > Blueprint** and connect the repository.
3. Render reads `render.yaml`. Enter secret values for `FIREBASE_SERVICE_ACCOUNT_JSON`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and your comma-separated frontend origins in `ALLOWED_ORIGINS`.
4. Deploy and verify `https://<your-render-host>/health`.

For a manually created Render Web Service, use:

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app.main:app -k uvicorn.workers.UvicornWorker --bind 0.0.0:$PORT`
- Health check path: `/health`

Use the Firebase service-account JSON as a Render secret environment variable. If the private key contains real newlines, paste the full valid JSON; JSON escaping must retain each newline as `\\n`.

## 5. Run with Docker

Create and populate `.env` first, then build and run the image:

```bash
docker build -t stylestack-api .
docker run --rm -p 8000:8000 --env-file .env stylestack-api
```

The API will be available at `http://localhost:8000`, with its health check at `http://localhost:8000/health`.

To use a different container port, set `PORT` and update the host mapping:

```bash
docker run --rm -p 8080:8080 --env-file .env -e PORT=8080 stylestack-api
```
