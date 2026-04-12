SERVER LLM RULES (BACKEND LOGIC)
These rules define how the LLM should behave when generating backend logic, API behavior, or system workflows.

1. Data Model Enforcement
The LLM must always follow the exact schema:

Users

id

email

created_at

Pets

id

user_id

name

type

photo_url

breed

birth_date

weight

chip_id

passport_number

color

is_neutered

notes

created_at

Vaccinations

id

pet_id

name

date

next_date

note

created_at

Medical Records

id

pet_id

type

date

description

notes

created_at

Notes

id

pet_id

text

created_at

Reminders

id

pet_id

type

title

date

status

note

created_at

2. Relationship Rules
The LLM must enforce:

User → Pets (1:N)

Pet → all other entities (1:N)

No orphan records

No cross‑user access

3. Reminder Automation Rules
When reminder is completed:

If type = Vaccination → create vaccination record

If type = Vet Visit → create medical record

If type = Medication/Treatment → create medical record

4. Vaccination Logic
When adding vaccination:

Suggest next_date based on vaccine type (if known)

If next_date exists → auto‑create reminder

5. Status Calculation Rules
Server must calculate:

Vaccination status

Reminder status transitions

6. API Behavior Rules
Always return minimal required fields

Never expose internal fields

Always validate pet_id ownership

Always return errors in allowed format:

“Something went wrong”

“Failed to save”

“Check your connection”

7. Security Rules
Never allow cross‑user data access

Always validate user_id → pet_id → entity_id chain

No public endpoints without auth