# Ragly API (server)

Backend for **Ragly** — simple, reliable storage for pet medical history, reminders, auth, and subscriptions.

> Product name: **Ragly** · Cloud Run service / Mongo DB names still use **petto** / **petto_dev**.

Sibling mobile app: `../client` (Expo).

---

## Stack

| Layer | Choice |
|--------|--------|
| Language | Python 3.11 |
| Framework | FastAPI + Uvicorn |
| Database | MongoDB (Motor async) |
| Auth | Firebase Admin (verify ID tokens); email OTP → custom token |
| Email OTP | Resend (`RESEND_*`) or SMTP; else OTP logged in development |
| Push | Expo Push API (`app/core/push.py`) |
| Billing | RevenueCat webhook → user `subscription` |
| Deploy | Docker → Google Cloud Run (`europe-west1`) |
| Secrets | Env / Secret Manager (`google-cloud-secret-manager`) |

Public base (production example):

`https://petto-server-326582782489.europe-west1.run.app`

All app routes: **`/api/v1/...`**. Liveness: **`GET /health`**.

---

## Layout

```
app/
  main.py              FastAPI entry, CORS, routers, /health
  core/                config, DB, Firebase, email, push, scheduling, utils
  models/              Pydantic request/response models
  routers/             auth, users, pets, vaccinations, medical_records,
                       reminders, notifications, subscriptions
docs/                  Setup notes (subscriptions, Firebase package)
llm/server-rules.md    Product/API constraints for agents
Dockerfile
requirements.txt
.env.example           Local / petto_dev
.env.production.example  Cloud Run checklist (petto)
```

---

## Data (MongoDB)

Database name is selected by **`MONGODB_DB_NAME`**:

| Env | Typical DB | Purpose |
|-----|------------|---------|
| Local / client-dev | `petto_dev` | Safe experiments |
| Cloud Run production | `petto` | Real users |

### Collections

| Collection | Role |
|------------|------|
| `users` | Firebase UID, email, timezone, notification prefs, subscription |
| `email_otps` | One active OTP per email (TTL on `expires_at`) |
| `pets` | Pet profiles per `user_id` |
| `vaccinations` | Per pet; status computed on read |
| `medical_records` | Topics (“Medical History”) per pet |
| `health_notes` | Notes under a medical record (optional linked reminder) |
| `reminders` | Date/time/repeat; stored status + `notified_at` |
| `push_tokens` | Expo push tokens per user/device |

Indexes are created on startup (`app/core/database.py`).

### Reminder semantics

- **Stored** status: `scheduled` | `completed` | `missed`
- **API display** status: `today` | `scheduled` | `missed` | `completed` (from date vs user “today”)
- Tabs: `today` / `upcoming` / `recent` — **by calendar date**, not clock time
- Push + `notified_at` are set only when **`POST /api/v1/internal/dispatch-reminders`** runs (Cloud Scheduler), not when a document is edited in Compass

---

## API surface (`/api/v1`)

| Area | Prefix | Notes |
|------|--------|--------|
| Auth | `/auth` | `send-otp`, `verify-otp`, `resend-otp` (public) |
| Users | `/users` | `POST/GET/DELETE /me` (Bearer Firebase token) |
| Pets | `/pets` | CRUD |
| Vaccinations | `/pets/{id}/vaccinations` | CRUD |
| Medical | `/pets/{id}/medical-records` | Topics + nested notes |
| Reminders | `/pets/{id}/reminders` | CRUD + `PATCH .../status` |
| Notifications | `/notifications/...` | Register token, prefs |
| Dispatch | `/internal/dispatch-reminders` | Secret header only |
| Subscriptions | `/subscriptions/webhook` | RevenueCat Bearer secret |

Authenticated routes expect:

`Authorization: Bearer <Firebase ID token>`

---

## Setup (local)

```bash
cd server
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # fill values; keep MONGODB_DB_NAME=petto_dev
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

Health check: `GET http://localhost:8080/health`

Point the client’s `EXPO_PUBLIC_API_BASE_URL` at this host (use LAN IP on a physical phone, not `localhost`).

### Important env vars

| Variable | Purpose |
|----------|---------|
| `MONGODB_URI` / `MONGODB_DB_NAME` | Atlas connection + DB name |
| `FIREBASE_*` | Admin SDK (prefer `FIREBASE_PRIVATE_KEY_BASE64` on Cloud Run) |
| `CLIENT_APP_URL` / `DEEP_LINK_*` | Links / branding context (`https://ragly.cloud` in prod) |
| `RESEND_API_KEY` / `RESEND_FROM_EMAIL` | OTP email (`Ragly <noreply@ragly.cloud>`) |
| `INTERNAL_TASK_SECRET` | Required for dispatcher; Cloud Scheduler sends `X-Internal-Secret` |
| `DEFAULT_TIMEZONE` | Fallback when user has no tz |
| `REVENUECAT_WEBHOOK_SECRET` | Webhook auth |
| `PLAY_REVIEW_EMAIL` / `PLAY_REVIEW_OTP` | Fixed OTP for Play review login |

Full lists: `.env.example`, `.env.production.example`.

---

## Production / Cloud Run

- Image built from `Dockerfile` (Uvicorn on `$PORT`, default 8080)
- Set env/secrets from `.env.production.example` (`MONGODB_DB_NAME=petto`, `APP_ENV=production`)
- Do **not** point the production scheduler at `petto_dev`

### Reminder push (Cloud Scheduler)

```
POST https://<cloud-run-host>/api/v1/internal/dispatch-reminders
Header: X-Internal-Secret: <INTERNAL_TASK_SECRET>
```

Optional dry run: `?dry_run=true` (preview due items without sending).

Scheduler must target the **same** service/DB as the app under test. Production job → `petto` only.

### RevenueCat

```
POST /api/v1/subscriptions/webhook
Authorization: Bearer <REVENUECAT_WEBHOOK_SECRET>
```

See `docs/subscription-setup.md`.

---

## Manual dispatch (dev)

When testing against `petto_dev`, either run the API locally and curl it, or temporarily hit a Cloud Run revision that uses `petto_dev` (usually not worth a permanent second scheduler):

```bash
curl -X POST "http://localhost:8080/api/v1/internal/dispatch-reminders?dry_run=true" \
  -H "X-Internal-Secret: YOUR_SECRET"

curl -X POST "http://localhost:8080/api/v1/internal/dispatch-reminders" \
  -H "X-Internal-Secret: YOUR_SECRET"
```

---

## Related docs

- `llm/server-rules.md` — schema and API constraints for agents
- `docs/subscription-setup.md` — RevenueCat webhook
- `docs/firebase-package-alignment.md` — Firebase / package IDs
- `../client/README.md` — mobile app

---

## Product principles (short)

Data correctness, simplicity, predictability, security. No search layers, dashboards, or over-engineered abstractions — see `llm/server-rules.md`.
