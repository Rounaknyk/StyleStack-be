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

If the project already exists and the API reports that an onboarding profile
column does not exist, run the focused
[`202607150001_add_onboarding_profiles.sql`](supabase/migrations/202607150001_add_onboarding_profiles.sql)
migration in the SQL Editor. It is idempotent and preserves existing profiles.

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

### Local logs

The backend writes authentication, request, wardrobe-item, upload, deletion, and wear-log events to the terminal running Uvicorn. Firebase tokens, service keys, image contents, and user emails are never logged. A successful protected request looks like:

```text
2026-07-11 13:45:26 | INFO | stylestack.auth | firebase_user_authenticated uid=...
2026-07-11 13:45:26 | INFO | stylestack.wardrobe | wardrobe_items_listed uid=... count=2
2026-07-11 13:45:26 | INFO | stylestack.api | request_completed method=GET path=/api/v1/wardrobe/items status=200 duration_ms=125.4
```

Set `DEBUG=true` in `.env` for additional development detail. Keep it `false` in production.

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

Wardrobe categories include western and Indian ethnic pieces: `shirt`,
`pants`, `dress`, `jacket`, `shoes`, `accessory`, `kurta`, `saree`,
`lehenga`, `sherwani`, `salwar`, `dhoti`, `dupatta`, `blouse`, `anarkali`,
and `ethnic_set`. Vision tagging and Gmail import use the same vocabulary.

Wardrobe uploads return as soon as the compressed source image is safely stored
and the database record has been created. A backend worker then removes the
background, corrects phone-camera orientation, creates an optimized full JPEG
and an aspect-preserving 480 px thumbnail, and finally runs AI tagging. Flutter
polls `ai_tag_status` and never runs rembg/U2Net locally. Configure processing
with:

```env
BACKGROUND_REMOVAL_ENABLED=true
BACKGROUND_REMOVAL_MODEL=birefnet-general-lite
FASHION_SEGMENTATION_ENABLED=true
```

The removal model downloads on its first use. StyleStack preserves the original
canvas during removal and never crops thumbnails. If removal fails, the worker
keeps an optimized original instead of losing the upload.

Existing Supabase projects must run
[`202607150002_add_wardrobe_thumbnails.sql`](supabase/migrations/202607150002_add_wardrobe_thumbnails.sql)
once before testing this pipeline.

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
GET    /api/v1/wardrobe/items/{id}/tag-status
```

The update endpoint accepts a JSON object containing any editable fields. Wear logging accepts optional JSON such as:

```json
{
  "worn_at": "2026-07-11T10:00:00Z",
  "notes": "Office event"
}
```

### Background AI tagging

After an upload is stored and its database record is created, the API immediately places an image-processing job on an in-process queue and returns the item with `ai_tag_status: "pending"`. A daemon worker changes the status to `processing`, removes the background, optimizes the image, creates its thumbnail, calls vision AI, stores AI fields separately from user-editable fields, and finishes with `completed` or `failed`.

Configure Groq in `.env`:

```env
GROQ_API_KEY=your-groq-api-key
GEMINI_API_KEY=your-gemini-api-key
GEMINI_VISION_MODEL=gemini-flash-latest
GROQ_VISION_MODEL=qwen/qwen3.6-27b
GROQ_REQUEST_TIMEOUT_SECONDS=30
```

Poll a user-owned item without blocking uploads:

```bash
curl http://localhost:8000/api/v1/wardrobe/items/ITEM_ID/tag-status \
  -H "Authorization: Bearer YOUR_FIREBASE_ID_TOKEN"
```

Response:

```json
{"status":"pending"}
```

Possible statuses are `pending`, `processing`, `completed`, and `failed`. AI failures are retried up to three times. Run the updated `supabase/schema.sql` before uploading new items.

The Week 1 queue is intentionally process-local: queued jobs are lost if the process restarts, and multiple Gunicorn workers each have their own queue. Move to a durable queue such as Redis before production workloads require guaranteed processing.

### Outfit Selfies

The mobile app can photograph a worn outfit, match visible pieces against the
signed-in user's wardrobe, let the user correct every match, and then write the
confirmed pieces to `wear_logs`. Accepted selfies are retained in private
Supabase Storage and shown in the Profile outfit-history timeline. Low-quality
photos are rejected before anything is saved.

```text
POST /api/v1/wardrobe/outfit-selfies/analyze
POST /api/v1/wardrobe/outfit-selfies/{selfie_id}/confirm
DELETE /api/v1/wardrobe/outfit-selfies/{selfie_id}
GET  /api/v1/wardrobe/outfit-selfies/history
```

AI-generated `ai_visual_tags` are intentionally separate from editable tags.
They describe stable visual details used to match newly uploaded wardrobe items
in future selfies. Run the latest `supabase/schema.sql` once before testing this
feature; it adds the hidden tags and the two outfit-selfie tables.

### Gmail Closet Sync

Closet Sync searches only confirmed Amazon `Delivered:` emails. Each result is
validated again by sender and subject before processing; shipped, arriving,
failed, returned, refunded, and promotional messages are ignored. Related mail
for the same order is used to recover the purchased product title and Amazon
transactional thumbnail. Recommendation carousels, logos, and non-fashion
products are rejected without making vision-AI calls. Thumbnail URLs are
upgraded to their original-resolution catalog images before upload to private
Supabase Storage. Email HTML and temporary image bytes are not persisted.

### Weather-aware outfit API

Set these values in `.env`:

```env
OPENWEATHER_API_KEY=your-openweather-api-key
OPENWEATHER_BASE_URL=https://api.openweathermap.org/data/2.5
PEXELS_API_KEY=your-pexels-api-key
PEXELS_BASE_URL=https://api.pexels.com/v1
PEXELS_REQUEST_TIMEOUT_SECONDS=8
INSPIRATION_CLIP_ENABLED=false
INSPIRATION_CLIP_MODEL=openai/clip-vit-base-patch32
INSPIRATION_CLIP_THRESHOLD=0.28
INSPIRATION_CLIP_REQUEST_TIMEOUT_SECONDS=12
```

When configured, outfit suggestions also include up to two optional Pexels
style references. They are searched from the suggested wardrobe categories,
colors, occasion, and ethnic/western style context. Inspiration failures never
block outfit generation. Keep the Pexels key server-side and rotate any key
that has been pasted into chat, source control, or logs.

Before returning a reference, StyleStack applies a conservative metadata gate.
It rejects catalog/logo/flat-lay results, requires a worn-person signal, and
requires every distinct wardrobe category to be represented. For stronger
visual validation, install the optional local CLIP stack and enable it:

```bash
pip install -r requirements-clip.txt
```

With `INSPIRATION_CLIP_ENABLED=true`, each metadata-approved image is fetched
and scored locally against a description of the complete suggested outfit.
Images below `INSPIRATION_CLIP_THRESHOLD` are rejected, and CLIP failures are
fail-closed (the image is not shown). The first request downloads the selected
Hugging Face model, so keep this disabled on small instances unless the extra
memory and disk are available. The threshold is a raw cosine-similarity value,
not a calibrated human-percentage score; tune it using real examples.

Generate an outfit using owned wardrobe items, current weather, occasion, and wear history:

```http
POST /api/v1/outfits/suggest
Authorization: Bearer <firebase-id-token>
Content-Type: application/json

{"city":"Mumbai","occasion":"work"}
```

Mark every item in an outfit as worn:

```http
POST /api/v1/outfits/{outfit_id}/wear
Authorization: Bearer <firebase-id-token>
```

### Canvas Style Builder

The Flutter canvas lets a user arrange owned wardrobe pieces with drag, scale,
and rotate gestures. Saving captures the canvas as a PNG and persists the
layout JSON plus a private Supabase Storage preview:

```text
POST   /api/v1/canvas/styles
PUT    /api/v1/canvas/styles/{id}
GET    /api/v1/canvas/styles
GET    /api/v1/canvas/styles/{id}
DELETE /api/v1/canvas/styles/{id}
```

`POST` is multipart form data with `name`, `items` (a JSON array containing
`item_id`, `x`, `y`, `scale`, and `rotation`), and `preview_image`. The API
checks every item belongs to the Firebase-authenticated user before saving.
Run [`202607150003_add_canvas_styles.sql`](supabase/migrations/202607150003_add_canvas_styles.sql)
and [`202607150004_add_wardrobe_cutouts.sql`](supabase/migrations/202607150004_add_wardrobe_cutouts.sql)
before using this feature on an existing Supabase project. New wardrobe uploads
also receive a transparent `cutout_url`; the canvas uses it when available and
falls back to the optimized image for older items.

### Morning notifications

The Flutter app stores city, IANA timezone, notification time, and opt-in state through:

```text
GET  /api/v1/users/me/preferences
PUT  /api/v1/users/me/preferences
POST /api/v1/users/me/devices
```

The local scheduler checks enabled profiles every minute and uses Firebase Admin to send the generated outfit to registered devices. For iOS, upload an APNs authentication key in Firebase Console under **Project Settings > Cloud Messaging** and test on a physical signed device. The simulator does not provide a production push-notification test.

The scheduler is process-local for MVP development. Set `NOTIFICATION_SCHEDULER_ENABLED=false` on extra API workers or replace it with one durable scheduled worker before horizontally scaling.

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
