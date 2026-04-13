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
- Add “smart AI features”

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

When reminder is marked as **completed**:

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

- Accept:
  name, date, optional next_date

- IF next_date exists:
  → auto-create Reminder

- IF vaccine type is known:
  → suggest next_date (do NOT enforce)

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

Client must NOT override server truth.

---

## 4. API Design Rules

### 4.1 Response Principles

- Return ONLY required fields
- Do NOT expose internal fields
- Use consistent response shape

### 4.2 Errors (STRICT)

Only allowed messages:

- "Something went wrong"
- "Failed to save"
- "Check your connection"

No custom backend messages.

---

### 4.3 Validation

Always validate:
- pet_id belongs to user
- required fields exist
- dates are valid

Reject invalid data.

---

## 5. Security Rules (CRITICAL)

- No cross-user access EVER
- All endpoints require authentication
- Validate ownership chain:
  user → pet → entity

- Never trust client input blindly

---

## 6. Data Philosophy

The system exists to:

- NEVER lose data
- ALWAYS return consistent data
- Be predictable for the user

Do NOT:
- Guess user intent
- Auto-edit user data
- Modify past records silently

---

## 7. Offline Support Logic

- Accept delayed writes
- Ensure idempotency
- Avoid duplicate records

---

## 8. Performance Rules

- Keep queries simple
- Prefer indexed queries by:
  pet_id
  user_id
  date

- Avoid heavy joins / aggregations

---

## 9. What NOT to Build

DO NOT implement:

- Search
- AI recommendations
- Complex scheduling logic
- Predictive features

---

## 10. Success Condition

The backend is correct if:

- Data is always accurate
- User never loses information
- System behaves predictably
