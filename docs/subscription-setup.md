# Subscription setup (RevenueCat)

Code is ready. Purchases stay unavailable until you finish the store + RevenueCat checklist below.

## IDs (keep these exact)

| What | Value |
|------|--------|
| Entitlement | `petto_premium` |
| Store product | `petto_premium_monthly` |
| Price | $2.99 / month |

## Client env (`client/.env`)

```
EXPO_PUBLIC_REVENUECAT_IOS_KEY=appl_...
EXPO_PUBLIC_REVENUECAT_ANDROID_KEY=goog_...
```

Restart Metro after changing env (`npx expo start -c`). IAP does **not** work in Expo Go — use an EAS development or production build.

## Server env

```
REVENUECAT_WEBHOOK_SECRET=<same secret you set in RevenueCat webhooks>
```

Webhook URL:

`https://<your-api>/api/v1/subscriptions/webhook`

Authorization header: `Bearer <REVENUECAT_WEBHOOK_SECRET>`

## App Store Connect

1. Paid Apps Agreement + banking/tax
2. Subscription group → product `petto_premium_monthly` at $2.99/mo
3. Sandbox testers
4. In-App Purchase capability on the iOS app

## Google Play Console

1. Upload a signed AAB (at least internal testing)
2. Subscription `petto_premium_monthly` monthly base plan $2.99
3. License testers
4. Activate the product

## RevenueCat

1. Add iOS + Android apps (bundle / package = your Expo ids)
2. Connect App Store Connect API key + Play service account
3. Entitlement `petto_premium` → product `petto_premium_monthly`
4. Default offering with the monthly package
5. Webhook as above; App User ID = Firebase UID (`Purchases.logIn(uid)`)

## Free limits (enforced now)

- 1 pet
- 10 active reminders (today + upcoming across all pets)

Premium users are unlimited. Plan is mirrored on the user document via the webhook.
