# PETTO — SERVER LLM RULES (BACKEND)

## 0. Core Principle
This is NOT a complex system.
This is a **simple, reliable data storage system for pet medical history**.

Priorities:
1. Data correctness
2. Simplicity
3. Predictability
4. Security

DO NOT:
- Add unnecessary abstractions
- Over-engineer logic
- Add smart AI features
- Add search or filtering systems
- Add predictive behavior

---

## 1. Data Model (STRICT)

The LLM MUST follow this schema exactly.

Users
- id
- email
- created_at

Pets
- id
- user_id
- name
- type
- photo_url
- breed
- birth_date
- weight
- chip_id
- passport_number
- color
- is_neutered
- notes
- created_at

Vaccinations
- id
- pet_id
- name
- date
- next_date
- note
- created_at

MedicalRecords
- id
- pet_id
- type
- date
- description
- notes
- created_at

Notes
- id
- pet_id
- text
- created_at

Reminders
- id
- pet_id
- type
- title
- date
- status
- note
- created_at

DO NOT:
- Rename fields
- Add hidden fields
- Add extra entities

---

## 2. Relationships (MANDATORY)

- User → Pets (1:N)
- Pet → all other entities (1:N)

Rules:
- No orphan records
- No cross-user access
- Every query must validate ownership:
  user_id → pet_id → entity_id

---

## 3. Core System Behavior

### 3.1 Reminder → Record Automation

When reminder is marked as completed:

IF type = "Vaccination"
→ create Vaccination record

IF type = "Vet Visit"
→ create MedicalRecord

IF type = "Medication" OR "Treatment"
→ create MedicalRecord

This must happen automatically.

---

### 3.2 Vaccination Logic

When creating vaccination:
- accept name, date, optional next_date
- if next_date exists → auto-create Reminder
- if vaccine type is known → suggest next_date, do not enforce

---

### 3.3 Status Calculation (SERVER IS SOURCE OF TRUTH)

Server MUST calculate:

Vaccination:
- Up to date
- Due soon
- Overdue

Reminder:
- Scheduled
- Today
- Missed
- Completed

Client must not override server truth.

---

## 4. Date and Time Rules

Server must enforce:
- local-time-compatible date storage and interpretation
- reminder default time = 09:00 when relevant
- records use past dates
- reminders use future dates

Date format displayed on client may vary, but server validation rules must enforce valid date semantics.

---

## 5. Auth and Ownership Rules

Auth flow assumptions:
- email is required
- account exists before protected data access
- all protected operations require authenticated user identity
- incomplete onboarding may produce authenticated user with incomplete pet data, but not unrestricted access to unrelated entities

Server must always validate:
- authenticated user
- pet ownership
- entity ownership chain

---

## 6. API Design Rules

### 6.1 Response Principles
- Return only required fields
- Do not expose internal fields
- Use consistent response shapes
- Keep payloads lightweight

### 6.2 Errors (STRICT)
Only allowed user-facing messages:
- "Something went wrong"
- "Failed to save"
- "Check your connection"

No custom backend user-facing messages.

### 6.3 Validation
Always validate:
- pet_id belongs to user
- required fields exist
- dates are valid
- text length max = 300 where applicable

Reject invalid data.

---

## 7. Sorting Rules

Default sorting:
- Vaccinations → newest first
- MedicalRecords → newest first
- Notes → newest first
- Reminders → Today, Scheduled, Missed, Completed

Server should provide or support data in these expected orders where relevant.

---

## 8. Security Rules (CRITICAL)

- No cross-user access ever
- All endpoints require authentication
- Validate ownership chain: user → pet → entity
- Never trust client input blindly
- Do not expose secrets
- Do not expose internal implementation details

---

## 9. Data Philosophy

The system exists to:
- never lose data
- always return consistent data
- remain predictable
- support trust and recall

Do NOT:
- guess user intent
- silently change records
- modify past records automatically
- auto-correct medical history without explicit user action

---

## 10. Offline Support Logic

Server must tolerate delayed synchronization.

Rules:
- accept delayed writes
- ensure idempotency where possible
- avoid duplicate records from repeated sync attempts
- preserve consistency during reconnect scenarios

---

## 11. Performance Rules

- Keep queries simple
- Prefer indexes by:
  - pet_id
  - user_id
  - date
- Avoid heavy joins
- Avoid unnecessary aggregations
- Avoid large over-fetched payloads

---

## 12. What NOT to Build

DO NOT implement:
- Search
- Filters
- AI recommendations
- Complex scheduling engines
- Predictive features
- Analytics engines for MVP
- Extra derived entities beyond the documented model

---

## 13. Success Condition

The backend is correct if:
- data is accurate
- ownership is always enforced
- reminders and records stay consistent
- offline sync does not create confusing duplicates
- the client receives predictable, minimal, trustworthy data

## 14. Testing Rules (STRICT)

The LLM must generate tests ONLY for critical backend logic.

Testing is required for:
- data validation
- ownership validation (user → pet → entity)
- reminder → record automation
- status calculation logic

DO NOT:
- generate excessive test suites
- test UI behavior
- test trivial getters/setters
- add complex mocking frameworks

---

### 14.1 Test Scope

Tests must cover:

1. Ownership validation
- user cannot access another user's pet
- invalid pet_id must be rejected

2. Data validation
- required fields missing → rejected
- invalid dates → rejected
- text length > 300 → rejected

3. Reminder automation
- completing reminder creates correct record
- correct entity type is created

4. Status logic
- vaccination status is correct
- reminder status is correct

---

### 14.2 Test Style

- Keep tests simple and readable
- One behavior per test
- No deep abstraction
- No unnecessary setup

---

### 14.3 Test Structure

Tests must be placed in:

/tests/

Example:
- test_users.py
- test_pets.py
- test_reminders.py

---

### 14.4 Test Environment

- Use a test database (not production)
- Do not rely on external services
- Keep tests deterministic

---

### 14.5 Success Condition

Tests are correct if:
- critical logic is protected
- invalid data is rejected
- ownership is enforced
- automation behaves predictably