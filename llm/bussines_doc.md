Ragly for Business
Full Developer Specification — Web Dashboard first, then the mobile discovery home

This is the main product step. It is an addition to the existing Ragly pet app. Nothing already shipped is removed: pets, vaccinations, reminders, health notes, and the owner account stay. The mobile app gets a navigation and style adjustment later. The first thing to build is the website.

Document Ragly for Business — Developer Specification
Platform Responsive website for businesses and Ragly admins. Mobile app remains the pet-owner product.
Initial market Chișinău / Moldova
Website users Business owners, their team, and Ragly admins (the founders and partners)
Mobile users Pet owners. They discover published businesses. They do not get the business dashboard.
Backend Shared Ragly FastAPI + MongoDB (this server)
Development approach One picture, then build it together. Production-quality. No second backend.

1. Product vision
Ragly becomes a two-sided product in the same way Wolt connects places and people: businesses publish themselves, Ragly reviews them, and pet owners find those businesses in the mobile app.

Core relationship: Pet owner ↔ Pet ↔ Published business

The website is where a business is created, reviewed, and managed.
The mobile app is where a pet owner discovers a published business and keeps using the pet tools they already have.

Future, after profiles are live: services, offers, reviews, then calendar and booking. Do not make booking the first dependency.

Do not build in the first version: accounting, payments, inventory, payroll, a full CRM, complex analytics, chat, AI, or a veterinary medical-record system for businesses.

2. What stays
The current pet-owner product stays on the phone: pets, vaccines, reminders, health, auth, subscriptions.
A person with a pet can also request to publish a business.
A person with a business can also have pets in the mobile app.
Same Ragly account. Same Firebase login. Same user record.

3. Who sees what
Surface Who What they see
Public welcome screen Anyone The product presentation, then log in or register. Later, paid businesses can show their Ragly reviews here, before login.
Business dashboard Signed-in user Their own draft, review status, public profile, and later team, services, offers
Ragly admin Signed-in founder or partner The review queue, approve or reject, and the ability to publish a business for an owner
Mobile app Pet owner Only businesses with status Published, inside the category home. Profile holds the old home.

The landing page is not the dashboard. A visitor can read the product page without an account. Login and register lead into the dashboard. The admin queue is a separate area on the same site, visible only to Ragly admins.

Pet owners do not browse businesses on the website. Discovery is in the app.

4. One account
There is no separate “business owner account” and “pet owner account”.
• New email on the website: register, then they can submit a business.
• Email that already uses the mobile app: same login on the website, then they can submit a business.
• Email that already has a business: same login on the phone, then they can add a pet.
If the submission is not a real business, Ragly rejects it. The account remains. The business does not go live.

5. Website shape
Build this first.

5.1 Welcome screen
This is the public landing page, before any business account login. It presents Ragly: what the product is, how the system works, and a paragraph that asks business owners to publish their business in the app. Actions on this screen: log in, or register if they have no Ragly account yet.
No business form on this screen. No admin queue on this screen.

5.1.1 Paid review banners — later
After the welcome screen exists, a business that pays Ragly can have its reviews shown on this same page, still before login.
• The banner is public. A visitor does not need an account to see it.
• The reviews are that business’s own Ragly reviews. Never Google reviews.
• Only a Published business can be featured.
• Ragly admins choose which paying businesses appear. A business cannot turn its own banner on.
• The banner does not open the dashboard. Log in and register stay separate actions.
• The first version of the welcome screen has no banners and no payment checkout. Leave a clear place on the page so the banners can be added without rebuilding the product story.

5.2 Business dashboard
After login. This is the working surface.
If they have no business yet, the primary action is to add one.
The form must be clear, with short instructions on each field, and it must refuse “Submit for review” while a required field is missing. Saving a draft may be incomplete. Sending for review may not.

5.3 Ragly admin
Same website, different area. Founders and partners review publish requests the way Apple and Google review an app before it is listed.
They see the same fields the owner filled, the current status, and they approve or reject.
They can also create and publish a business on behalf of an owner, for listings the team already has (the clinic examples).

6. Registration and review
Steps for a business owner:
1. Open the landing page.
2. Register or log in with the existing Ragly account.
3. Fill the business form in the dashboard.
4. Submit for review only when every required field is valid.
5. Status becomes Pending Review. The business is not in the mobile app.
6. Ragly admins receive that submission in the admin queue. They see the owner’s data. They do not see other owners’ private pet data.
7. Approve: status becomes Published, the submitting user becomes the Owner, and the business appears in the mobile app under its category.
8. Reject: status becomes Rejected, the owner sees the reason, and they can edit and submit again. Use this when the business is not real or the listing is incomplete or unfit.

The owner dashboard and the admin queue must show the same status and the same submitted fields. The owner cannot set their own status to Published. The phone app cannot list a business that is not Published.

7. Ragly admin publishes for an owner
Founders and partners can enter a business themselves and set it Published without waiting for that owner to fill the form. This is how the existing clinic list can go into the app.
• If that owner already has a Ragly account, the admin can attach them as Owner.
• If not, the listing can be Published with no Owner yet. Claim comes later.
• This path is not the worker invitation. Worker invitations still start only from the Owner after the business exists.

8. Security rules that decide if this works
Access mistakes here break the product.
• A signed-in user reads and edits only businesses where they have a business_members row, plus their own unsubmitted draft.
• They never receive another business’s form, team, or review notes.
• Status changes (approve, reject, suspend, admin-publish) happen only on Ragly-admin endpoints.
• The browser must not be trusted. The server checks the Firebase ID token, then either Ragly-admin allowlist or business membership, and the Mongo query is filtered by that.
• Submit-for-review runs the same required-field checks on the server. Hiding the button is not enough.
• The mobile app receives public fields of Published businesses only. It does not receive member emails, invitations, or rejection notes.
• Ragly admin is an allowlist of founder and partner accounts. It is not the business role named Admin. A business Admin cannot open the review queue.
• Never expose Firebase Admin, MongoDB, or Cloud Run credentials to the website or the app. Clients call `/api/v1/...` with a user ID token only.

Two different “admin” words:
• Ragly admin — founders and partners. Review and publish.
• Business Admin — a team role the Owner assigns. Can edit the public profile. Cannot invite and cannot review other businesses.

9. Team invitations
Unchanged in intent. Only after someone is the Owner.
Inside the dashboard the Owner edits the team.
• Enter an email and a role: business Admin or Worker.
• More than one email means more than one invitation.
• Add business worker sends one email per address through Resend.
• That email asks the person to register or log in, then approve joining this business.
• The request goes only from the Owner to that worker. A business Admin or Worker cannot invite. A person cannot ask to join.
• They must sign in with the invited email. Membership is created only when they approve.
• Decline, revoke, or expiry creates no access.
• The Owner can change a member’s role or remove them. That does not delete their Ragly account.

Roles:
• Owner — the approved submitter, or the user a Ragly admin attached. One Owner in this version. Not granted by a worker invitation.
• Business Admin — profile, services, offers. No team management.
• Worker — day-to-day access defined for that role. No team management.

Membership is `business_members`: business_id, user_id, email, role. Do not rely on a single owner_user_id.

10. Business object
This is the record both the website form and the mobile listing use. Field names below are the stored names.

Public contact email is not the login email. Login lives on the user account. `email` here is the business inbox, and it may be absent.

Field Required to submit for review Notes
name Yes Public name
phones Yes, at least one List of strings. One clinic can have three numbers. Do not store a single phone string.
email No Business contact email. Omit when they have none.
description No Short public text
category Yes One of the category ids below
city Yes First market: Chișinău
address Yes Street address as shown publicly
timezone Yes IANA name. Moldova listings use Europe/Chisinau
opening_hours Yes Either always open, or all seven weekdays
latitude Yes Decimal. Required so the app can place the business
longitude Yes Decimal
website No Omit when they have none
photo_url No One public image in Firebase Storage. A desktop filename is only a source file, not the stored value
instagram No Optional extra link
status System Owner never types this. Draft, Pending Review, Published, Rejected, Suspended

Drafts may omit required fields. Submit for review and Ragly-admin publish may not. The form lists what is still missing and keeps Submit disabled until the server would accept it.

Category ids for the first home screen:
• veterinarian — vets
• groomer — groomers
• pharmacy — pharmacies
• pet_friendly — pet friendly places
• pet_store — pet stores

Same object for every category. Trainer, pet hotel, and pet sitter can be added later as new category ids without a new collection.

opening_hours is stored on the business document. There is no separate hours collection in this version.

Always open, as with Ciavdar Grup:

```json
{
  "always_open": true
}
```

A normal week, as with Nicoleta-Lux and VetAsist. Times are local to `timezone`, 24-hour `HH:MM`. Each day is a list of ranges so a split day can exist later. An empty list means closed.

```json
{
  "always_open": false,
  "mon": [{ "open": "08:00", "close": "17:00" }],
  "tue": [{ "open": "08:00", "close": "17:00" }],
  "wed": [{ "open": "08:00", "close": "17:00" }],
  "thu": [{ "open": "08:00", "close": "17:00" }],
  "fri": [{ "open": "08:00", "close": "17:00" }],
  "sat": [{ "open": "08:00", "close": "13:00" }],
  "sun": []
}
```

Stored shape for the three examples:

Ciavdar Grup Clinica Veterinară — open all day, one phone, has a contact email, no website.

```json
{
  "name": "Ciavdar Grup Clinica Veterinară",
  "phones": ["+373 22 518 983"],
  "email": "ciavdar@mail.ru",
  "description": "Cеть ветеринарных клиник Ciavdar - это современная ветеринарная медицина и большая любовь к животным.",
  "category": "veterinarian",
  "city": "Chișinău",
  "status": "published",
  "address": "Strada Nicolae H. Costin 61, Chișinău",
  "timezone": "Europe/Chisinau",
  "opening_hours": { "always_open": true },
  "latitude": 47.04002294512967,
  "longitude": 28.776049324432268,
  "photo_url": null
}
```

Medical-Veterinarian Center of Nicoleta-Lux — no contact email, no website, Sunday closed.

```json
{
  "name": "Medical-Veterinarian Center of Nicoleta-Lux",
  "phones": ["+373 22 922 805"],
  "description": "Clinică Veterinară avînd medici specialiști experimentați și dedicați.",
  "category": "veterinarian",
  "city": "Chișinău",
  "status": "published",
  "address": "Calea Orheiului 59, Chisinau, Moldova",
  "timezone": "Europe/Chisinau",
  "opening_hours": {
    "always_open": false,
    "mon": [{ "open": "08:00", "close": "17:00" }],
    "tue": [{ "open": "08:00", "close": "17:00" }],
    "wed": [{ "open": "08:00", "close": "17:00" }],
    "thu": [{ "open": "08:00", "close": "17:00" }],
    "fri": [{ "open": "08:00", "close": "17:00" }],
    "sat": [{ "open": "08:00", "close": "13:00" }],
    "sun": []
  },
  "latitude": 47.0457537263822,
  "longitude": 28.84798196552696,
  "photo_url": null
}
```

VetAsist Clinica Veterinară — three phones, no contact email, has a website.

```json
{
  "name": "VetAsist Clinica Veterinară",
  "phones": ["+373 22 221 303", "(022) 78-03-02", "(067) 32-32-11"],
  "description": "VetAsist. Clinica veterinara. Toate serviciile veterinare.",
  "category": "veterinarian",
  "city": "Chișinău",
  "status": "published",
  "address": "Vasile Lupu 59, Chisinau, Moldova, 2000",
  "timezone": "Europe/Chisinau",
  "opening_hours": {
    "always_open": false,
    "mon": [{ "open": "09:00", "close": "17:00" }],
    "tue": [{ "open": "09:00", "close": "17:00" }],
    "wed": [{ "open": "09:00", "close": "17:00" }],
    "thu": [{ "open": "09:00", "close": "17:00" }],
    "fri": [{ "open": "09:00", "close": "17:00" }],
    "sat": [{ "open": "09:00", "close": "13:00" }],
    "sun": []
  },
  "website": "https://vetasist.com.md/",
  "latitude": 47.02171888564807,
  "longitude": 28.80045480000002,
  "photo_url": null
}
```

`photo_url` stays empty in these examples until the real image is uploaded. The files ciadvar.png, nicoleta.png, and vetassist.png are desktop sources for that upload, not values stored in Mongo.

Form instructions:
• Phones: at least one. Button to add another. Show them as a list.
• Hours: a choice of “Open 24/7” or a week grid. Sunday may be closed. Submit needs either always open or all seven days filled (closed counts as filled).
• Email, website, description, photo: labeled optional. Empty is valid.
• Category, city, address, map coordinates, timezone: required, with a short reason (“Pet owners see this in the app”).
• Timezone defaults to Europe/Chisinau and stays visible.

11. Status the owner and the admin both see
Draft → Pending Review → Published
Rejected returns to the owner with a reason. They can edit and submit again, which sets Pending Review.
Suspended hides a live listing. Archived is a later end state.

Status Meaning Visible in the app Owner sees it Ragly admin sees it
Draft Still editing No Yes Only if they open that business
Pending Review Waiting No Yes, with the same fields Yes, in the queue
Published Approved Yes Yes Yes
Rejected Not a fit, or not ready No Yes, plus the reason Yes
Suspended Hidden after being live No Yes Yes

12. Dashboard navigation
Overview, Business profile, Team, then later Services, Offers, Reviews, Calendar, Clients, Settings.
The first build can ship Overview, Business profile, and the review state. Team follows as soon as an Owner exists. Services and the rest stay in this spec so the data model is not painted into a corner.

Overview shows the business name, the live status, what is missing before submit, and the primary action: finish the profile, or wait for review, or edit the published profile.

13. Services, offers, reviews — after the listing is real
Services are the base for later booking. Fields: name (required), description, price in MDL, duration in minutes, active, sort order. No bundles, deposits, taxes, or dynamic pricing.

Offers are informational: title, description, start, end, active, optional image. No coupons or payments.

Ratings and reviews are Ragly’s own. Never copy Google ratings, reviews, photos, or descriptions. The business can read them and report one. They cannot edit or delete a user’s review.

14. Calendar, booking, clients — later
Calendar starts manual: day view, working hours taken from opening_hours, appointments with client name, pet name, service, time, and status.
Booking later: the pet owner picks a published business, a service, a time, and a pet. The server checks availability. Time is stored with the business timezone.
Clients appear from bookings. A business does not receive the pet’s health record unless the owner explicitly shares it. That permission model is later.

15. Collections
Same Mongo database as `users`, `pets`, `reminders`, `vaccinations`.

Entity Purpose
users Existing Ragly account. Add display_name for the pet-owner greeting.
businesses The object in section 10
business_members Owner, business Admin, Worker
business_invitations Email invites. Not membership until accepted.
business_services Later
business_offers Later
business_ratings Later
business_reviews Later

Do not add business_working_hours or a separate phone table. Hours and phones live on `businesses`.

business_members: id, business_id, user_id, email, role, created_at, updated_at. One membership per user per business. The Owner row is created on approve, or when a Ragly admin attaches an owner.

business_invitations: id, business_id, email, role, invited_by_user_id, status, created_at, expires_at, responded_at.
Statuses: Pending, Accepted, Declined, Revoked, Expired.
One Pending invite per email per business. invited_by_user_id is the Owner. role is business Admin or Worker. Accepting creates the membership. Opening the email does not.

users.display_name: the pet owner’s own name, collected on the phone the first time that email registers, before the pet’s name. The app then greets them by that name. The website does not ask for it.

16. Mobile app — specified now, built after the website
Screen styling comes in a later pass. Behavior is fixed here so the website fields match the app.
• The current home becomes Profile. The profile button opens that screen. Pets, vaccines, reminders, and health stay there.
• The new home is the categories: vets, groomers, pharmacies, pet friendly places, pet stores. Each list shows Published businesses of that category.
• First registration: if this email is not registered yet, ask for the pet owner’s name, then continue to the pet’s name. Returning emails skip that step.
• Greeting copy can say hello using display_name.
• A business in Draft, Pending Review, Rejected, or Suspended is absent from these lists.

17. Visual theme
The website uses the mobile light palette. Do not invent a second brand.
• Background #F6F7F9
• Cards #FFFFFF
• Primary text #1F2937
• Secondary text #6B7280
• Border #E5E7EB
• Primary button #004741, label #FFFFFF
• Disabled button #8DB0AA
• Error #EF4444
• Success #84CC9D

Tone: calm, short labels, empty states that say what to do next. Same words as the app.

Responsive: the welcome screen, the later review banners, and the business dashboard must work on a phone, a tablet, and a desktop browser. One layout that adapts. On a narrow screen, stack sections and form fields, keep buttons full width, and do not force horizontal scrolling. On a wide screen, the same pages use the extra room without stretching text into long lines.

17.1 Cache
Use cache everywhere it makes the next visit feel immediate. The server stays the source of truth.
• Welcome screen and other public pages: cache at Cloudflare. A repeat visit should not rebuild the product story.
• Images, including later review-banner photos: cache in the browser and on the CDN.
• Dashboard and admin data: TanStack Query, the same client cache the Expo app uses. Business profile, review status, team, services, offers, and reviews stay on screen when the user moves between pages. Refresh in the background instead of showing an empty page.
• After a save, submit, approve, reject, invite, or remove, update that cached record immediately so the new status is what they see.
• A signed-in session survives a refresh. Send the user to log in again only when the session is actually expired.
• Private dashboard and admin responses stay in that browser only. Do not put them on a shared public cache.

18. Empty states
Screen Copy
Dashboard, no business You have no business yet. Add your business to request a place in the Ragly app.
Profile, missing fields These fields are required before we can review your business.
Pending review We are reviewing your business. You will see the result here.
Rejected We could not publish this business. The reason is shown. Edit it and submit again.
Team No workers yet. Add a worker by email. They join only after they accept.
Services No services yet. Add your first service so pet owners can see what you offer.
Offers No offers yet.
Reviews No reviews yet. Reviews appear when Ragly users rate your business.

19. Maps and outside data
Store latitude and longitude on the business. The first website form collects them with the address. No Google Places API and no scraped Google listing. Directions in the app can open Apple Maps or Google Maps.
Do not copy Google reviews, ratings, photos, or descriptions.

20. Email that is in scope now
• Invitation when the Owner adds a worker.
• Optional later: mail the owner when the review is approved or rejected. The dashboard status is the source of truth even if that mail is not built yet.

Booking notifications stay later.

21. Stack
Layer Use
Mobile Existing Expo app at `c:\apps\petto\client`, TypeScript
Website Next.js and TypeScript at `c:\apps\petto\web-client`, same language as the Expo app. Welcome screen, business dashboard, Ragly admin. Same API. No JavaScript UI files.
API Existing FastAPI on Cloud Run, europe-west1
Database Existing MongoDB. Local petto_dev, production petto
Auth Existing Firebase. Email OTP and the same user on web and mobile
Files Existing Firebase Storage bucket, CORS already includes https://business.ragly.cloud
Email Existing Resend
Hosting Website on Cloudflare at business.ragly.cloud. API stays on Cloud Run

No Supabase, Postgres, second auth, or second database.

22. Build order
The website dashboard is first. Mobile home styling waits until Published businesses exist to show.
1. Business document, membership, invitations, and display_name on users.
2. Welcome screen separate from the logged-in dashboard. Product story and login only. Paid review banners come later.
3. Shared login and register.
4. Business form with the required-field gate and the hours and phones shapes above.
5. Submit for review. Owner sees status. Ragly admin sees the same submission.
6. Approve, reject with reason, and admin-create a Published business.
7. Owner team invitations.
8. Services, offers, reviews.
9. Mobile: owner name on first registration, category home, old home moved to Profile, lists of Published businesses.
10. Calendar and booking only after listings are in real use.

23. Done when
• The welcome screen explains Ragly and asks businesses to publish. It is public, and it is not the dashboard. Paid review banners are not part of this first screen.
• Public pages are cached. Dashboard data stays on screen between pages and updates in the background. Private data is not on a shared public cache.
• One account works on the website and in the app, including someone who already has a pet.
• The form matches section 10. Submit stays blocked until required fields are valid, on the server as well as in the browser.
• Pending, Published, and Rejected look the same to the owner and to Ragly admin.
• Approve makes that user the Owner and shows the business in the app category.
• Reject does not show it. A fake or unfit business can be rejected.
• Ragly admins can publish a complete business themselves.
• A business user cannot open another business or the review queue.
• A business Admin cannot invite workers. Only the Owner can, and the worker must accept.
• Phones are a list. Hours are always-open or a seven-day schedule. Contact email and website may be empty.
• Existing pet features are still in the product.

24. Later checklist
Shared backend ☐
Welcome screen vs dashboard ☐
Cache on public pages and in the dashboard ☐
Paid review banners on the welcome screen ☐
Shared pet and business account ☐
Business object and submit validation ☐
Review queue and status sync ☐
Ragly admin publish-for-owner ☐
Team invitations ☐
Services, offers, reviews ☐
Mobile category home and Profile move ☐
Pet owner display_name ☐
Calendar and booking ☐

25. Product line
Maps answer where a place is. Ragly should answer which place to choose for this pet, and what the owner can do there.
For a business: be reviewed once, appear to pet owners in the app, and manage that listing from the website.
