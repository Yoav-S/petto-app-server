# Firebase package alignment + Analytics → RevenueCat

Canonical IDs for Petto (do not mix):

| What | Value |
|------|--------|
| Firebase project | `petto-494013` |
| Android / iOS package | `com.yoav.petto` |
| Expo `app.json` | `com.yoav.petto` |
| JS Auth (`.env`) | `EXPO_PUBLIC_FIREBASE_*` → `petto-494013` |
| Android Firebase App ID | `1:326582782489:android:111685b98d4163feedf888` |
| iOS Firebase App ID | `1:326582782489:ios:a9a315c3bebfc797edf888` |

Native config files (in `client/`):
- `google-services.json` (Android)
- `GoogleService-Info.plist` (iOS)

---

## RevenueCat → Firebase Analytics

Use the App IDs above + Measurement Protocol API secrets from the matching GA data streams:

1. Analytics → Admin → Data streams → iOS / Android for `com.yoav.petto`
2. Create API secrets if needed
3. RevenueCat → Integrations → Firebase → paste App IDs + secrets
4. Enable sandbox events → Save  
5. Leave Web + Extension empty

---

## App code

After `Purchases.logIn`, the app sets `Purchases.setFirebaseAppInstanceID(...)` from `@react-native-firebase/analytics`.

Requires a **dev/production build** (not Expo Go):

```bash
cd client
eas build --profile development --platform android
```

Log to look for:

```text
[Subscription] set Firebase Analytics appInstanceId for RC → GA
```

---

## Google Sign-In note

If Google login breaks after the package rename, update Firebase / Google Cloud OAuth clients to bundle/package **`com.yoav.petto`** and add the EAS SHA-1 for Android.
