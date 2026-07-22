# Subscription setup (RevenueCat)

Petto uses **Test Store** until you publish. Real App Store / Play products come later.

## IDs (must match)

| What | Value |
|------|--------|
| Entitlement | `petto_premium` |
| Product (Test Store) | `sub_premium` |
| Offering | `default` → Monthly package → `sub_premium` |
| Price | $2.99 / month |
| App User ID | Firebase UID (`Purchases.logIn(uid)`) |

## Step 1 — Client keys (`client/.env`)

RevenueCat → Project settings → **API keys** → copy the **Test Store** / public SDK key (`test_…`).

```
EXPO_PUBLIC_REVENUECAT_IOS_KEY=test_...
EXPO_PUBLIC_REVENUECAT_ANDROID_KEY=test_...
```

(Same `test_` key on both platforms is OK for Test Store.)

Restart Metro: `npx expo start -c`

After login you should see Metro logs like:

```
[Subscription] SDK configured ...
[Subscription] logged in appUserId=...
[Subscription] READY — offering/package wired ...
```

## Step 2 — Server webhook secret (`server/.env` + Cloud Run)

Pick any long random string (e.g. password manager), then set:

```
REVENUECAT_WEBHOOK_SECRET=your_long_random_secret
```

Redeploy / restart the API so Cloud Run has the same secret.

## Step 3 — RevenueCat webhook

RevenueCat → **Project** → **Integrations** → **Webhooks** → Add:

- URL: `https://petto-server-326582782489.europe-west1.run.app/api/v1/subscriptions/webhook`
  (or your custom API domain if you map one)
- Authorization: `Bearer <same REVENUECAT_WEBHOOK_SECRET>`
- Events: leave defaults (purchases, renewals, cancellations, expirations)

Send a **test event** from the RC webhook UI. Server logs should show the webhook; Mongo user `subscription` updates when `app_user_id` matches a Firebase UID.

### Manual webhook smoke test

Replace `FIREBASE_UID` with a real user uid from Firebase Auth / Mongo:

```bash
curl -X POST "https://petto-server-326582782489.europe-west1.run.app/api/v1/subscriptions/webhook" \
  -H "Authorization: Bearer YOUR_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d "{\"api_version\":\"1.0\",\"event\":{\"type\":\"INITIAL_PURCHASE\",\"app_user_id\":\"FIREBASE_UID\",\"product_id\":\"sub_premium\",\"entitlement_ids\":[\"petto_premium\"],\"expiration_at_ms\":1893456000000}}"
```

Then `GET /api/v1/users/me` (authenticated) should show `"plan":"premium"`.

## Step 4 — What Firebase / domain are for

| Service | Role for subscriptions |
|---------|------------------------|
| Firebase Auth | User id becomes RevenueCat `app_user_id` — already wired |
| Firebase iOS/Android apps | Needed for the app build / Auth, **not** for RC Test Store |
| Domain (`peto.casa`) | Privacy/Terms links, email OTP, optional custom API host — **not** required for Test Store IAP |

You do **not** need to finish every Firebase console field to test Test Store subscriptions.

## Step 5 — Later (publish): real stores

1. App Store Connect + Play: create subscription (can keep store id `sub_premium` or map in RC)
2. RevenueCat: add iOS/Android apps, upload ASC API key + Play service account
3. Replace client keys with `appl_…` / `goog_…`
4. Keep entitlement `petto_premium` attached to the store products

## Free limits (already enforced)

- 1 pet
- 10 active reminders (today + upcoming)

## Native build note

SDK configure / purchase need a **dev or production build**. Expo Go will log configure failures. Webhook + Mongo plan updates can be tested today without a store listing.
