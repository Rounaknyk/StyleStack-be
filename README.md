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

### Tester access overrides

Run [`202607210001_add_access_overrides.sql`](supabase/migrations/202607210001_add_access_overrides.sql), then manage tester access directly in Supabase. The authenticated `GET /api/v1/users/me/access` endpoint matches only a verified Firebase email and exposes no admin credentials. The app refreshes this access on sign-in, resume, and before an ad gate, so changes do not require an app release.

```sql
insert into public.access_overrides (email, note)
values ('tester@example.com', 'Closed beta')
on conflict (email) do update set
  bypass_ads = true,
  enabled = true;

-- Revoke immediately on the user's next refresh/resume:
update public.access_overrides set enabled = false
where email = 'tester@example.com';
```

Phone-only accounts have no verified email and therefore cannot use the email allowlist. Keep this table private; the backend service-role client is its only reader.

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
POOF_API_KEY=your-server-side-poof-key
POOF_REQUEST_TIMEOUT_SECONDS=45
FASHION_SEGMENTATION_ENABLED=true
```

When `POOF_API_KEY` is configured, wardrobe jobs use Poof first for a fast,
high-quality transparent cutout. If Poof is unavailable or its credits are
exhausted, the same job automatically falls back to the local fashion
segmentation/BiRefNet pipeline; the upload is never rejected only because Poof
failed. Keep the key only in backend environment variables (for example, a
Render secret), never in Flutter or Git.

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

Pexels-powered outfit moodboards are optional and can be hidden globally with
no database lookup or app update. Set this on the backend and restart/deploy:

```env
PEXELS_INSPIRATION_ENABLED=false
```

When disabled, the API returns `inspiration_enabled: false` and the Flutter app
removes the **See the vibe** action. Existing outfit suggestions are unaffected.

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

### Groq request queue and duplicate reuse

Wardrobe preview analysis uses a fair, process-local queue. The queue exposes
position and ETA, allows queued jobs to be canceled/retried, and lets the mobile
app remain navigable while work continues:

```text
POST   /api/v1/wardrobe/analysis-jobs
GET    /api/v1/wardrobe/analysis-jobs/{job_id}
POST   /api/v1/wardrobe/analysis-jobs/{job_id}/retry
DELETE /api/v1/wardrobe/analysis-jobs/{job_id}
```

One process-wide rolling gate covers Groq calls from wardrobe vision, outfit
generation, and Gmail import. Its default is 30 requests in any rolling
60-second window. Wardrobe queue scheduling allows three jobs per user per
minute and temporarily skips a capped user's jobs so other users can progress;
it does not reject the extra jobs.

Before enqueueing, StyleStack creates a perceptual hash. Matching hashes reuse
the cached structured analysis without calling an AI provider. Run
`supabase/migrations/202607190001_add_ai_analysis_cache.sql` before enabling
this flow.

Groq 429 responses honor `Retry-After`, retry once through the same rolling
gate, and only then use the configured Gemini fallback. The queue is deliberately
in-memory for the pilot: jobs do not survive an API restart and each horizontally
scaled API instance would have its own 30-RPM gate. Use one API instance until
the queue moves to a shared durable worker/Redis.

### Wear history (Outfit Selfies disabled)

Outfit Selfies are intentionally disabled for the current MVP to avoid their
vision-AI and image-processing cost. Their API router is not mounted, so a
mobile client cannot start selfie analysis. The dormant implementation and
existing private data remain available for a possible future rollout.

The outfit timeline now reads ordinary `wear_logs`. Logging a suggested outfit
adds each of its wardrobe items with one timestamp, and the timeline groups
those records into a single look:

```text
GET /api/v1/wardrobe/wear-history
```

### Gmail Closet Sync

Closet Sync searches only confirmed Amazon `Delivered:` emails. Each result is
validated again by sender and subject before processing; shipped, arriving,
failed, returned, refunded, and promotional messages are ignored. Related mail
for the same order is used to recover the purchased product title and Amazon
transactional thumbnail. Recommendation carousels, logos, and non-fashion
products are rejected without making vision-AI calls. Thumbnail URLs are
upgraded to their original-resolution catalog images before upload to private
Supabase Storage. Email HTML and temporary image bytes are not persisted.

The app starts a complete import with `POST /api/v1/imports/gmail/jobs` and
polls `GET /api/v1/imports/gmail/jobs/{job_id}`. A single in-process worker
paginates through every matching delivered email and skips order IDs already in
the wardrobe before product extraction or AI enrichment. The short-lived Google
token exists only in worker memory and is cleared when the job completes. This
MVP queue survives app navigation but not a backend process restart; move it to
a durable database queue before running multiple API instances.

### Weather-aware outfit API

#### Professional Stylist Engine

Today's Outfit and Ask Your Stylist use a hybrid engine so basic dressing
sense is enforced in code instead of delegated to a single generative prompt:

```text
wardrobe + profile + 3-day wear history
                  |
          normalize garment roles
                  |
      build valid outfit formulas only
                  |
 deterministic compatibility + score
                  |
  visually distinct clothing combinations only
                  |
 remove recent looks, including the one
 explicitly supplied by the refresh action
                  |
       top 10 candidates (small JSON)
                  |
       one Groq text ranking request
                  |
      final validation -> saved outfit
                  |
     deterministic fallback if AI fails
```

Normalization derives role/type, subtype, colour, pattern, fabric, texture,
fit/silhouette, formality, season, style language, audience/neutrality and
Indian/western context from existing wardrobe metadata and AI tags.
The formula layer supports top + bottom, one-piece looks, optional footwear and
accessories, formal layering, kurta + salwar/dhoti/churidar, and saree/lehenga
+ blouse combinations. Hard gates reject incomplete looks, duplicate primary
roles, severe formality conflicts, uncontrolled pattern clashes, and arbitrary
sporty/formal or ethnic/western mixing.

Office intent is recognized from terms such as presentation, interview,
meeting, conference, client, corporate and boardroom—not only the literal word
`formal`. These requests pass through an additional credibility gate: the base
must contain an office-ready top or one-piece; utility/streetwear primary
pieces are excluded; layers must actually be blazers, suit jackets, waistcoats,
Nehru jackets or bandhgalas; and footwear/accessories must be suitable for
work. If the wardrobe lacks those finishing pieces, the engine returns a
simpler shirt-and-trouser look rather than mislabeling a hoodie, puffer, bomber,
Crocs or backpack as formal.

Surviving candidates receive a transparent 100-point score: completeness 22%,
formality coherence 16%, colour harmony 18%, silhouette 13%, texture/fabric 9%,
style coherence 10%, and occasion/personal fit 12%. Footwear can add a small
completion bonus.
Accessory-only variants do not consume multiple shortlist positions: shoes,
watches and bags cannot make an otherwise identical shirt-and-trouser pairing
count as a new look. Duplicate wardrobe rows with the same visible clothing
identity also collapse into one candidate instead of creating fake variety.
The refresh action sends its currently displayed outfit ID explicitly. The
engine combines that outfit with recent generated history and removes those
semantic clothing combinations before ranking, so an AI provider failure falls
back to the strongest unseen candidate rather than returning the same C1.
After every compatible combination has genuinely been exhausted, refresh
returns a clear message instead of silently repeating an outfit.

Backend logs make each decision inspectable without exposing image data. The
three strongest eligible candidates are emitted as `stylist_top_candidate`
lines containing local score, item names in brackets, item IDs and the full
score breakdown. `stylist_chosen` then records the final candidate, source
(`ai_ranked` or deterministic fallback), names, IDs and score breakdown.
The AI does **not** select arbitrary wardrobe IDs; it ranks these candidates and
returns one candidate ID plus the user-facing explanation. The final candidate
is validated again. An unavailable or malformed AI response falls back to the
highest local score, so outfit generation remains usable and costs at most one
stylist AI call.

The learning loop stores `worn`, `liked`, `refreshed`, `disliked`, and
`wore_something_else` signals. Recent signals become small per-item affinity
adjustments during local scoring, without another AI call. The existing Log
this outfit action records `worn`; refreshing records `refreshed`; logging an
alternate look records `wore_something_else`. Apply the idempotent migration
[`202607220002_add_outfit_feedback.sql`](supabase/migrations/202607220002_add_outfit_feedback.sql)
before deploying this engine.

```text
POST /api/v1/outfits/{outfit_id}/feedback
{"signal":"liked","reason":"Optional short note"}
```

Use `GROQ_STYLIST_MODEL` to change only the text ranking model independently of
the vision auto-tagger. No external benchmark suite is included in the MVP;
the deterministic rules and focused regression tests are the current safety
baseline and should later be calibrated against anonymized real user choices.

Set these values in `.env`:

```env
OPENWEATHER_API_KEY=your-openweather-api-key
OPENWEATHER_BASE_URL=https://api.openweathermap.org/data/2.5
GROQ_STYLIST_MODEL=qwen/qwen3.6-27b
PEXELS_API_KEY=your-pexels-api-key
PEXELS_BASE_URL=https://api.pexels.com/v1
PEXELS_REQUEST_TIMEOUT_SECONDS=8
PEXELS_RESULTS_PER_REQUEST=10
INSPIRATION_CLIP_ENABLED=false
INSPIRATION_CLIP_MODEL=openai/clip-vit-base-patch32
INSPIRATION_CLIP_THRESHOLD=0.28
INSPIRATION_CLIP_REQUEST_TIMEOUT_SECONDS=12
```

When configured, outfit suggestions make one Pexels search request per outfit
with up to ten candidates, then return every candidate that passes the quality
gates. There is no arbitrary limit on accepted references. They are searched
from the suggested wardrobe categories,
colors, occasion, and ethnic/western style context. Inspiration failures never
block outfit generation. Keep the Pexels key server-side and rotate any key
that has been pasted into chat, source control, or logs.

Local CLIP image/text similarity is available as an opt-in visual validator.
It is disabled by default because the model download is roughly 600 MB. When
disabled, StyleStack uses a lightweight metadata score (human/fashion context,
category coverage, color coverage, and catalog rejection terms). To enable CLIP
deliberately, install the optional stack:

```bash
pip install -r requirements-clip.txt
```

Each candidate is then fetched and scored locally against a description of the
complete suggested outfit. This requires downloading the configured model on
the first request.
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

### Owner broadcast notifications

StyleStack can send an announcement to every device whose user has opted into
notifications. Broadcasts support an optional public HTTPS image and a safe
in-app destination (`today`, `wardrobe`, `planner`, `profile`,
`notifications`, `saved_styles`, or a specific `outfit`). The endpoint is
protected by a server-only key; never include that key in Flutter or Git.

Configure Render with a long random value:

```bash
openssl rand -hex 32
```

Store the result as `ADMIN_NOTIFICATION_KEY` on Render, and export the same
value only in your local terminal when sending:

```bash
export STYLESTACK_ADMIN_NOTIFICATION_KEY='your-secret'
python scripts/send_broadcast_notification.py \
  --title 'A fresh StyleStack edit is ready' \
  --body 'Open the app to see what is new.' \
  --destination today
```

Add media when useful:

```bash
python scripts/send_broadcast_notification.py \
  --title 'Monsoon styling guide' \
  --body 'Tap to open your wardrobe.' \
  --destination wardrobe \
  --image-url 'https://your-public-cdn.example.com/monsoon.jpg'
```

Use `--dry-run` first to validate credentials and the FCM payload without
delivering it. Android uses StyleStack's monochrome status-bar icon. iOS always
uses the installed app icon; rich images require the included Notification
Service Extension and must be tested on a physical device.

Connected Google Calendars are refreshed automatically in the same background
scheduler. The default interval is five minutes, so a meeting added shortly
before it starts can appear without the user pressing Sync. Configure
`GOOGLE_CALENDAR_SYNC_INTERVAL_SECONDS` (minimum 60) or disable it with
`GOOGLE_CALENDAR_AUTO_SYNC_ENABLED=false`. This requires the API process to be
running; a sleeping/free web service cannot perform background work while it is
stopped.

### Ads and tester bypasses

The Flutter app has two rewarded placements: an extra daily outfit after two free refreshes, and the first Google Calendar connection. Tester overrides bypass ads. A user who dismisses the Calendar ad before earning the reward stays disconnected; SDK initialization, invalid AdMob configuration, network, load, or show failures fail open so an advertising outage never blocks the feature. Analytics records the bypass reason without logging personal data.

Run
[`202607220001_remove_subscription_access.sql`](supabase/migrations/202607220001_remove_subscription_access.sql)
after the access-override migration if the earlier subscription column was
created. Subscription support is intentionally deferred and is not part of the
current app.

## 4. Deploy to Render

### Free pilot mode (recommended while validating the product)

The backend defaults to `FREE_PILOT_MODE=true`. This is designed for a small
indie pilot and does not require Redis, a paid background worker, CLIP, or a
second Render service:

- image tagging gets one provider attempt instead of three retries;
- preview/detection calls are limited to three per user and operation per day;
- Gmail sync is capped at ten messages per request;
- repeated identical Pexels outfit requests are served from a 24-hour process
  cache, so only the first request uses the API;
- uploads still return immediately while the existing single worker processes
  images in the background.

These guardrails are process-local, so they protect a free single-instance
pilot but are not a distributed quota system. Jobs can still be lost if the
API process restarts. When usage or reliability requirements justify paid
infrastructure, set `FREE_PILOT_MODE=false` and move the worker/limits to a
durable queue and shared limiter.

The limits can be tuned without code changes:

```env
FREE_PILOT_MODE=true
FREE_PILOT_AI_DAILY_LIMIT=3
FREE_PILOT_GMAIL_MAX_MESSAGES=10
FREE_PILOT_INSPIRATION_CACHE_SECONDS=86400
```

This mode reduces avoidable provider calls and CPU bursts; it cannot guarantee
zero charges from third-party AI/API providers if their keys are enabled, so
keep provider quotas and billing alerts enabled in those consoles.

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
