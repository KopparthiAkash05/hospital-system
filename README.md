# Smart Hospital Management System

A full-stack **hospital appointment and management system** built with **Python (Flask)** and **MySQL**. It provides role-based web portals for **patients**, **doctors**, and **administrators** covering appointment scheduling, doctor availability and weekly schedules, department management, in-app notifications, automated email reminders, follow-up appointments, doctor availability search (including nearby-doctor search), symptom-based department suggestion, and an admin analytics dashboard with an audit trail.

The application is branded as **MediCare Hospital** inside the UI.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Key Features](#2-key-features)
3. [Technology Stack](#3-technology-stack)
4. [System Architecture](#4-system-architecture)
5. [Architecture Explanation](#5-architecture-explanation)
6. [System Workflow](#6-system-workflow)
7. [Role-Based Architecture](#7-role-based-architecture)
8. [Project Structure](#8-project-structure)
9. [Database Architecture](#9-database-architecture)
10. [API Architecture](#10-api-architecture)
11. [Authentication and Authorization](#11-authentication-and-authorization)
12. [Appointment Workflow](#12-appointment-workflow)
13. [Installation and Setup](#13-installation-and-setup)
14. [Backend Setup](#14-backend-setup)
15. [Database Setup](#15-database-setup)
16. [Frontend / Client Setup](#16-frontend--client-setup)
17. [Environment Variables](#17-environment-variables)
18. [Running the Application](#18-running-the-application)
19. [Testing](#19-testing)
20. [Security](#20-security)
21. [Future Enhancements](#21-future-enhancements)
22. [Project Limitations](#22-project-limitations)
23. [Screenshots](#23-screenshots)
24. [License](#24-license)
25. [Author / Project Information](#25-author--project-information)

---

## 1. Project Overview

The **Smart Hospital Management System** solves a very common real-world problem: hospitals and clinics still manage appointment booking through phone calls, paper registers, or spreadsheets, which leads to double-booking, lost records, no-shows, and no visibility for management.

This system provides a single web application where:

- **Patients** register once, search for doctors by department/specialization, check real availability, and book, reschedule, or cancel appointments online.
- **Doctors** register (or are added by an admin), publish their weekly working hours, and manage their appointment queue — approving, rejecting, completing, and scheduling follow-ups.
- **Administrators** oversee the whole system: doctors, patients, departments, schedules, appointments, and an audit log, and get an analytics dashboard with charts.

Each role logs in through a single login page and is redirected to its own **role-based dashboard**. All three roles share one database, so an action taken by one role (for example a doctor confirming an appointment) is immediately reflected for the others through status updates, in-app notifications, and email notifications.

The system improves appointment management by enforcing **slot-level uniqueness** (two live appointments can never occupy the same doctor + date + time), by computing availability dynamically from structured doctor schedules, and by keeping a complete **audit trail** of administrative actions.

---

## 2. Key Features

### Patient

- **Registration** — self-service signup with name, email, phone, age, gender, address, date of birth, and emergency contact (`/register`).
- **Login / logout** — unified login that identifies the account type automatically (`/login`).
- **Password recovery** — forgot-password email with a time-limited reset token (`/forgot_password`, `/reset_password/<token>`).
- **Dashboard** — upcoming appointments, today's appointments, appointment history, recent appointments, available doctors, and department list.
- **Doctor search** — browse/search doctors with filters for department, specialization, and free-text name search (`/patient/doctors`).
- **Department browsing** — list of active departments with doctor counts, plus a department detail page.
- **Doctor profile & availability** — full doctor profile (qualification, experience, license, consultation fee, description) with a rolling 14-day availability view.
- **Appointment booking** — book from the doctor profile (`/patient/book/<id>`) or from the multi-step booking page (`/patient/book_appointment`), with **live slot loading** (`/api/available_slots/<doctor_id>`).
- **Appointment management** — list, search, and view appointment details with status badges.
- **Cancellation** — cancel own appointments (`/patient/cancel/<id>`).
- **Rescheduling** — move an appointment to another date/time with re-validation of availability (`/patient/reschedule/<id>`).
- **Notifications** — in-app notification centre with unread counts (`/patient/notifications`).
- **Nearby doctors** — browser geolocation of the patient, Haversine distance ranking of doctors (`/patient/nearby_doctors`).
- **Symptom checker** — rule-based symptom → department suggestion (`/symptom_checker`).
- **Triage chatbot** — a rule-based chat widget on every page that recommends a department and links to doctor search (`/api/chatbot`).
- **Profile management** — update personal details and change password (`/patient/settings`).

> Not implemented: favourite doctors, prescription/medical records, payments.

### Doctor

- **Self-registration** — apply/register with specialization, department, qualification, license number, experience, consultation fee, profile description, address (auto-geocoded), and availability (`/doctor/register`).
- **Dedicated login** (`/doctor/login`) plus the unified `/login`.
- **Dashboard** — today's approved appointments, pending requests, upcoming appointments, completed history, cancelled/rejected counts, patient search, and date filtering.
- **Appointment confirmation / rejection** — approve or reject pending requests (`/doctor/approve/<id>`, `/doctor/reject/<id>`), with patient + doctor notifications and an approval email.
- **Appointment completion** — mark appointments completed, optionally scheduling a **follow-up in 1–15 days** (`/doctor/complete_appointment`), with a completion email to the patient.
- **Appointment cancellation** — cancel an appointment (`/doctor/cancel/<id>`).
- **Schedule management** — add, update, and delete structured weekly working hours (day, start/end time, slot duration, active flag) (`/doctor/schedule*`).
- **Notifications** — in-app notification centre (`/doctor/notifications`).
- **Profile management** — update profile, availability status (`Available` / `Unavailable`), and department; change password (`/doctor/settings`, `/doctor/change_password`).

> Not implemented: an approval queue for new doctor accounts — doctor registration is immediate; administrators control visibility through account activation (see Admin).

### Admin

- **Admin login** — dedicated `/admin/login` page (accounts live in the `Admin` table). A separate `/admin/register` page allows self-registration only when the correct server-side secret key (`ADMIN_SECRET_KEY`) is supplied.
- **Analytics dashboard** — totals for patients/doctors/departments/appointments, status breakdown, estimated revenue, department distribution (doughnut chart), monthly appointment growth (bar chart), and busiest doctors (Chart.js) (`/admin/dashboard`).
- **Doctor management** — list, add, edit, and activate/deactivate doctors (`/admin/doctors*`), plus per-doctor schedule management (`/admin/doctors/<id>/schedule`).
- **Patient management** — list, detail view, and activate/deactivate patients (`/admin/patients*`).
- **Doctor approval / rejection** — *implemented as activation control*: admins can deactivate a doctor account, which immediately blocks logins for that account and removes the doctor from bookable listings. (There is no separate "application pending" workflow.)
- **Department management** — add, edit, activate/deactivate, and delete departments (`/admin/departments*`).
- **Appointment management** — searchable, filterable appointment list (doctor, patient, department, date, status) with cancel action (`/admin/appointments*`).
- **Schedule management** — create, update, and delete any doctor's schedules (`/admin/schedules*`).
- **Appointment reports** — full appointment listing page (`/admin/reports`).
- **Follow-up appointments** — monitor the follow-ups doctors have scheduled and whether each reminder has been sent (`/admin/follow_ups`).
- **Notification & reminder monitoring** — browse all in-app notifications and the automated email-reminder queue (`/admin/notifications`).
- **Hospital information** — maintain the hospital's public name, contact details, opening hours, and about text (`/admin/hospital_info`).
- **Administrator accounts** — create additional admin accounts and activate/deactivate them (`/admin/admins`, `/admin/admins/add`, `/admin/admins/<id>/toggle`).
- **Email settings** — read-only view of the reminder/scheduler configuration (`/admin/email_settings`).
- **Audit logs** — searchable audit trail of user actions (`/admin/audit_logs`).
- **Profile management** — update own name and change own password (`/admin/profile`).

> The hospital-information page and additional administrator accounts require the `migration_admin_dashboard.sql` script (adds the `HospitalInfo` table and `Admin.is_active`).

---

## 3. Technology Stack

| Layer | Technology |
|---|---|
| Backend | Python 3 / Flask 2.3.3 |
| Templating | Jinja2 (server-side rendered pages) |
| Styling | Custom CSS3 design system (`static/css/style.css`) with light/dark theme |
| Client-side JS | Vanilla JavaScript (`fetch`, DOM), Chart.js (CDN) for admin analytics |
| Icons / Fonts | Font Awesome 6.4 and Google Fonts (Inter) via CDN |
| Database | MySQL 8 (hosted or local) |
| Database access | PyMySQL driver + parameterized raw SQL through a small Flask-`g` connection wrapper (no ORM) |
| Authentication | Flask session cookies + Werkzeug password hashing (pbkdf2 / scrypt) |
| Authorization | Custom role decorators (`patient_required`, `doctor_required`, `admin_required`, and JSON API variants) |
| Email delivery | Brevo SMTP API over HTTP (`BREVO_API_KEY`); Flask-Mail (Gmail SMTP) is configured as well |
| Background jobs | APScheduler (`BackgroundScheduler`) — appointment reminders and follow-up reminders |
| External services | OpenStreetMap Nominatim (geocoding of doctor addresses) |
| HTTP client (server) | `requests` (Brevo API, Nominatim) |
| HTTP client (browser) | `fetch` API |
| Production server | Gunicorn (`Procfile`: `web: gunicorn app:app`) |
| Version control | Git / GitHub |
| Editor | Visual Studio Code |

> Note: `requirements.txt` also lists `Flask-MySQLdb`, `sendgrid`, and `resend`; the running code actually uses a custom PyMySQL wrapper for the database and the Brevo HTTP API for email.

---

## 4. System Architecture

The application follows a **three-tier, server-rendered architecture**: a Flask application serves both the HTML pages (Jinja2 templates) and a JSON REST API from the same process, which talks directly to MySQL.

```text
                    ┌───────────────────────────────┐
                    │            Users              │
                    │                               │
                    │    Patient  |  Doctor  | Admin│
                    └───────────────┬───────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │        Web Browser            │
                    │                               │
                    │  Jinja2-rendered Pages        │
                    │  Templates + CSS + Vanilla JS │
                    │  Theme toggle / Nav / Forms   │
                    │  Live slot loading (fetch)    │
                    │  Chart.js admin analytics     │
                    │  Triage chatbot widget        │
                    └───────────────┬───────────────┘
                                    │
                        HTTP  |  HTML pages
                        fetch |  JSON REST API  (/api/*)
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │       Flask Backend           │
                    │        (app.py)               │
                    │                               │
                    │  Routes / Controllers         │
                    │  Session Authentication       │
                    │  Role Authorization           │
                    │  Slot & Availability Logic    │
                    │  Validation                   │
                    │  Notifications / Audit Logs   │
                    │  Email Helpers                │
                    │  APScheduler Reminder Jobs    │
                    └───────────────┬───────────────┘
                                    │
                            PyMySQL (parameterized SQL)
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │        MySQL Database         │
                    │                               │
                    │  Patient    Doctor    Admin   │
                    │  Department Appointment       │
                    │  DoctorSchedule               │
                    │  Notification    AuditLog     │
                    └───────────────────────────────┘

        Side integrations: Brevo (email API) | Nominatim (geocoding)
```

---

## 5. Architecture Explanation

### Presentation Layer

- **Templates** — `templates/` contains a shared `base.html` layout (navigation, notifications badge, theme toggle, chatbot widget) plus role-specific folders: `patient/`, `doctor/`, `admin/`.
- **Pages & forms** — server-rendered forms for login, registration, booking, rescheduling, profile editing, schedule editing, and admin CRUD.
- **Styling** — one custom stylesheet (`static/css/style.css`, ~63 KB) implementing a responsive design system with CSS variables and a light/dark theme persisted in `localStorage`.
- **Client scripts** — `static/js/main.js` (mobile nav, alert auto-hide, client-side required-field validation, smooth scroll, theme toggle, and a live 12-hour preview beside native time inputs) and `static/js/slot-refresh.js` (loads free time slots for a chosen doctor/date from `/api/available_slots/<id>` and blocks Sundays client-side).
- **Time format** — every user-visible time is shown in 12-hour `HH:MM AM/PM` form (zero-padded, e.g. `09:00 AM`, `12:00 PM`, `12:00 AM`) via the `get_12hr_time` helper and the `time12`/`dt12` Jinja filters. Stored values, slot generation, comparisons, reminders, and `<input type="time">` values stay 24-hour (`HH:MM`) for correctness; native time inputs also get an AM/PM preview without changing their underlying value.

### API / Communication Layer

- **Two communication styles from the same frontend:**
  1. **Classic form POSTs** — most pages submit HTML forms and receive a redirected, re-rendered page with flash messages.
  2. **`fetch` JSON calls** — used for live slot loading, the triage chatbot, and by external clients hitting the `/api/*` endpoints.
- **JSON REST API** — a set of `/api/...` endpoints returning `{success, message, data}` payloads for programmatic access (see [API Architecture](#10-api-architecture)).
- **Response flow** — request → route handler → validation/authorization → SQL → JSON or re-rendered template.

### Backend Layer

- **Single application module** — `app.py` (~5,000 lines) contains configuration, the database wrapper, decorators, helpers, all routes, email senders, and the scheduler.
- **Authentication** — session-based (`session['loggedin']`, `session['id']`, `session['user_type']`).
- **Authorization** — route decorators restrict access per role and return proper 401/403 JSON responses on API routes.
- **Business logic** — slot generation from `DoctorSchedule` (with fallback to free-text availability), slot collision checks, bookability checks (`is_active`, `availability_status`), duplicate-booking prevention, status transitions, notification fan-out, and audit logging.
- **Validation** — email format regex, required fields, past-date rejection, slot membership checks, password length/confirmation checks, department existence/active checks.
- **Background services** — APScheduler cron jobs run every minute to send appointment reminders (30 minutes ahead) and follow-up reminders on the follow-up date.

### Data Layer

- **No ORM** — direct SQL via PyMySQL with parameterized queries (`%s` placeholders) and `DictCursor` rows.
- **Connection lifecycle** — one connection per request, stored in Flask's `g`, closed by a `teardown_appcontext` hook.
- **Schema management** — plain SQL scripts (`schema.sql` for core tables, `migration_smart_hospital.sql` for the smart-hospital feature tables/columns); no migration framework.
- **Integrity** — foreign keys with `ON DELETE CASCADE` / `SET NULL`, unique email keys, and a generated `slot_key` column with a unique index so two live appointments can never share doctor + date + time.

---

## 6. System Workflow

```text
User
  ↓
Login / Registration
  ↓
Authentication (session + hashed password)
  ↓
Role Identification (patient | doctor | admin)
  ↓
Role-Based Dashboard
  ↓
Feature Selection
  ↓
Form POST  /  fetch() API Request
  ↓
Flask Route (validation + authorization)
  ↓
Database Operation (parameterized SQL)
  ↓
Side Effects: Notification + Audit Log + Email (if applicable)
  ↓
Response (redirect + rendered page  |  JSON)
  ↓
Frontend Update
```

In plain language: a user registers or logs in, the server verifies the password hash and stores the identity in the session, the role decorator decides which pages that session may access, and every feature request is validated and authorized on the server before any row is read or written. Results are pushed back either as a re-rendered page (browser flows) or JSON (slot loading, chatbot, `/api/*` clients), after which notifications, audit entries, and emails are triggered where relevant.

---

## 7. Role-Based Architecture

| Role | Main Responsibilities |
|---|---|
| **Patient** | Register/login, search doctors and departments, view availability, book / reschedule / cancel appointments, view status updates and notifications, nearby-doctor search, symptom checker, manage own profile |
| **Doctor** | Register/login, manage weekly schedule and availability status, approve / reject / cancel appointments, complete appointments and schedule follow-ups, view notifications, manage own profile and password |
| **Admin** | Login, view analytics dashboard, manage doctors (add/edit/activate-deactivate) and their schedules, manage patients, manage departments, manage all appointments, view reports and audit logs, manage own profile |

---

## 8. Project Structure

Repository: `hospital-system` (Flask application root — there is no separate frontend/backend split).

```text
hospital-system/
├── app.py                          # Entire Flask application (config, routes, logic, scheduler)
├── requirements.txt                # Python dependencies
├── Procfile                        # Gunicorn process definition for deployment
├── schema.sql                      # Core tables: Patient, Doctor, Admin, Appointment
├── migration_smart_hospital.sql    # Feature tables: Department, DoctorSchedule,
│                                   #   Notification, AuditLog + column additions
├── migration_admin_dashboard.sql   # Admin additions: HospitalInfo table,
│                                   #   Admin.is_active + reminder columns
├── freedb_xor9rxN1 (1).sql         # phpMyAdmin dump: reference snapshot with sample data
├── details.env                     # Local secrets (gitignored — you create this)
├── .gitignore                      # Ignores details.env, .env, __pycache__
├── static/
│   ├── css/
│   │   └── style.css               # Full application design system (light/dark theme)
│   └── js/
│       ├── main.js                 # Nav, alerts, validation, theme toggle
│       └── slot-refresh.js         # Live availability loading for booking/reschedule
└── templates/
    ├── base.html                   # Shared layout: navbar, chatbot, theme toggle
    ├── index.html                  # Public landing page
    ├── login.html                  # Patient login (doctors/admins have their own pages)
    ├── register.html               # Patient registration
    ├── forgot_password.html        # Password reset request
    ├── reset_password.html         # Password reset via token
    ├── notifications.html          # Shared notification list renderer
    ├── symptom_checker.html        # Symptom → department suggestion
    ├── patient/
    │   ├── dashboard.html
    │   ├── view_doctors.html
    │   ├── doctor_profile.html
    │   ├── departments.html
    │   ├── department_detail.html
    │   ├── book_appointment.html
    │   ├── book_appoinment_page.html
    │   ├── appointments.html
    │   ├── appointment_detail.html
    │   ├── reschedule.html
    │   ├── nearby_doctors.html
    │   └── settings.html
    ├── doctor/
    │   ├── login.html
    │   ├── register.html
    │   ├── dashboard.html
    │   ├── appointments.html
    │   ├── appointment_detail.html
    │   ├── schedule.html
    │   └── settings.html
    └── admin/
        ├── dashboard.html          # Analytics dashboard (Chart.js)
        ├── doctors.html
        ├── doctor_form.html
        ├── doctor_schedules.html
        ├── patients.html
        ├── patient_detail.html
        ├── appointments.html
        ├── follow_ups.html         # Follow-up appointments monitoring
        ├── notifications.html      # Notification & reminder monitoring
        ├── departments.html
        ├── schedules.html
        ├── reports.html
        ├── audit_logs.html
        ├── hospital_info.html      # Public hospital details
        ├── admins.html             # Administrator account management
        ├── email_settings.html     # Reminder / scheduler configuration
        └── profile.html
```

> The backend is intentionally a **single-module Flask app**: everything lives in `app.py`, organized into commented sections (configuration → decorators → helpers → routes → email → scheduler → JSON API → entrypoint).

---

## 9. Database Architecture

MySQL database created by `schema.sql` and extended by `migration_smart_hospital.sql`.

### Tables

| Table | Purpose | Key columns |
|---|---|---|
| **Patient** | Patient accounts | `patient_id` (PK), `name`, `email` (unique), `password` (hash), `phone`, `age`, `gender`, `address`, `date_of_birth`, `emergency_contact_name`, `emergency_contact_phone`, `is_active`, `reset_token`, `reset_token_expiry` |
| **Doctor** | Doctor accounts & profiles | `doctor_id` (PK), `name`, `email` (unique), `password` (hash), `specialization`, `experience`, `phone`, `address`, `latitude`, `longitude`, `available_slots` (free-text fallback), `department_id` (FK), `qualification`, `license_number`, `consultation_fee`, `profile_description`, `availability_status`, `is_active`, `reset_token`, `reset_token_expiry` |
| **Admin** | Administrator accounts | `admin_id` (PK), `name`, `email` (unique), `password` (hash), `is_active`, `created_at` |
| **Appointment** | Booking records | `appointment_id` (PK), `patient_id` (FK), `doctor_id` (FK), `date`, `time`, `status` enum (`Pending`, `Approved`, `Rejected`, `Completed`, `Cancelled`, `No-Show`), `reason`, `department_id` (FK snapshot), `follow_up_date`, `reminder_sent`, `follow_up_reminder_sent`, `slot_key` (generated column) |
| **Department** | Clinical departments | `department_id` (PK), `name` (unique), `description`, `is_active` |
| **DoctorSchedule** | Structured weekly availability | `schedule_id` (PK), `doctor_id` (FK), `day_of_week` (ISO 1=Mon…7=Sun), `start_time`, `end_time`, `slot_duration` (minutes), `is_active` |
| **Notification** | In-app notifications | `notification_id` (PK), `user_type` (`patient`/`doctor`/`admin`), `user_id`, `title`, `message`, `appointment_id` (FK), `is_read` |
| **AuditLog** | Admin audit trail | `audit_id` (PK), `user_type`, `user_id`, `user_name`, `action`, `entity`, `entity_id`, `description`, `created_at` |
| **HospitalInfo** | Public hospital details (single row) | `info_id` (PK), `name`, `tagline`, `address`, `phone`, `emergency_phone`, `email`, `opening_hours`, `about`, `updated_at` |

### Relationships

```text
Department
 └── Doctor  (Doctor.department_id → Department, ON DELETE SET NULL)

Doctor
 ├── DoctorSchedule  (DoctorSchedule.doctor_id → Doctor, ON DELETE CASCADE)
 └── Appointment     (Appointment.doctor_id → Doctor, ON DELETE CASCADE)

Patient
 └── Appointment     (Appointment.patient_id → Patient, ON DELETE CASCADE)

Department
 └── Appointment     (Appointment.department_id → Department, snapshot at booking)

Appointment
 ├── Notification    (Notification.appointment_id → Appointment, ON DELETE CASCADE)
 └── Follow-up       (Appointment.follow_up_date — same row, visited again later)
```

### Important constraints

- **`DoctorSchedule`** — unique on `(doctor_id, day_of_week, start_time)`.
- **`Appointment.slot_key`** — a stored generated column equal to `doctor_id-date-time` for `Pending`/`Approved` rows and `NULL` otherwise, with a **unique index** (`uq_appointment_slot`). Because MySQL unique indexes ignore `NULL`, cancelled/rejected/completed appointments release their slot for rebooking while double-booking a live slot is impossible at the database level.

> **Note for fresh databases:** the *Nearby Doctors* feature reads and writes `Patient.latitude` / `Patient.longitude` (set by browser geolocation). These columns are not created by `schema.sql` / `migration_smart_hospital.sql`, so add them if you start from an empty database:
> `ALTER TABLE Patient ADD COLUMN latitude DECIMAL(10,8) NULL, ADD COLUMN longitude DECIMAL(11,8) NULL;`

---

## 10. API Architecture

The Flask app exposes two kinds of endpoints:

1. **Server-rendered routes** (`/`, `/patient/...`, `/doctor/...`, `/admin/...`) — HTML pages for the browser.
2. **JSON REST API** (`/api/...`) — machine-readable responses using the same session-based authentication, shaped as `{ "success": bool, "message": str, ... }`.

### JSON REST API

| Module | Endpoint | Method | Purpose | Access |
|---|---|---|---|---|
| Auth | `/api/patient/register` | POST | Register a patient | Public |
| Auth | `/api/patient/login` | POST | Patient login (sets session) | Public |
| Auth | `/api/doctor/login` | POST | Doctor login (sets session) | Public |
| Auth | `/api/logout` | POST | Clear the session | Public |
| Doctors | `/api/doctors` | GET | List doctors (optional `?specialization=`), password fields excluded | Public |
| Availability | `/api/available_slots/<doctor_id>` | GET | Free slots for `?date=YYYY-MM-DD` (`&exclude=<appointment_id>`) | Public |
| Appointments | `/api/patient/bookAppointment` | POST | Book an appointment (doctor, date, time, reason) | Patient |
| Appointments | `/api/patient/appointments` | GET | Patient's appointments | Patient |
| Appointments | `/api/doctor/appointments` | GET | Doctor's appointments (`?status=` filter) | Doctor |
| Appointments | `/api/approveAppointment` | PUT | Approve / reject an appointment | Doctor |
| Admin | `/api/admin/dashboard` | GET | Dashboard counters | Admin |
| Admin | `/api/admin/addDoctor` | POST | Create a doctor | Admin |
| Admin | `/api/admin/removeDoctor` | DELETE | Delete a doctor | Admin |
| Assist | `/api/chatbot` | POST | Rule-based triage conversation (step machine) | Public |
| Ops | `/api/health` | GET | Health check | Public |

### Key server-rendered routes

| Module | Route | Methods | Purpose |
|---|---|---|---|
| Public | `/` | GET | Landing page |
| Auth | `/login` | GET, POST | Patient login (checks the `Patient` table only) |
| Auth | `/register` | GET, POST | Patient registration |
| Auth | `/doctor/login`, `/doctor/register` | GET, POST | Doctor login / registration |
| Auth | `/admin/login`, `/admin/register` | GET, POST | Admin login / secret-key-gated registration |
| Auth | `/forgot_password`, `/reset_password/<token>` | GET, POST | Password reset flow |
| Auth | `/logout` | GET | Session logout |
| Patient | `/patient/dashboard` | GET | Patient dashboard |
| Patient | `/patient/doctors`, `/patient/doctor/<id>` | GET | Doctor list & profile with availability |
| Patient | `/patient/departments`, `/patient/department/<id>` | GET | Department list & detail |
| Patient | `/patient/book/<doctor_id>`, `/patient/book_appointment` | GET, POST | Book an appointment |
| Patient | `/patient/appointments`, `/patient/appointment/<id>` | GET | Appointment list & detail |
| Patient | `/patient/reschedule/<id>` | GET, POST | Reschedule |
| Patient | `/patient/cancel/<id>` | GET, POST | Cancel |
| Patient | `/patient/notifications` | GET | In-app notifications |
| Patient | `/patient/nearby_doctors`, `/patient/update_location` | GET / POST | Geolocation-based doctor search |
| Patient | `/patient/settings` | GET, POST | Profile & password |
| Doctor | `/doctor/dashboard` | GET | Doctor dashboard |
| Doctor | `/doctor/appointments`, `/doctor/appointment/<id>` | GET | Appointment list & detail |
| Doctor | `/doctor/approve/<id>`, `/doctor/reject/<id>`, `/doctor/cancel/<id>` | GET | Status transitions |
| Doctor | `/doctor/complete_appointment` | POST | Complete + optional follow-up date |
| Doctor | `/doctor/schedule`, `/doctor/schedule/add`, `/doctor/schedule/<id>/update`, `/doctor/schedule/<id>/delete` | GET/POST | Weekly schedule management |
| Doctor | `/doctor/settings`, `/doctor/change_password` | GET/POST, POST | Profile & password |
| Doctor | `/doctor/notifications` | GET | In-app notifications |
| Admin | `/admin/dashboard` | GET | Analytics dashboard |
| Admin | `/admin/doctors`, `/admin/doctors/add`, `/admin/doctors/<id>/edit`, `/admin/doctors/<id>/toggle`, `/admin/doctors/<id>/schedule` | GET/POST | Doctor CRUD, activation, schedules |
| Admin | `/admin/patients`, `/admin/patients/<id>`, `/admin/patients/<id>/toggle` | GET/POST | Patient list, detail, activation |
| Admin | `/admin/appointments`, `/admin/appointments/<id>/cancel` | GET/POST | Appointment management |
| Admin | `/admin/departments` (+ `/add`, `/<id>/edit`, `/<id>/toggle`, `/<id>/delete`) | GET/POST | Department management |
| Admin | `/admin/schedules` (+ `/add`, `/<id>/update`, `/<id>/delete`) | GET/POST | Schedule management |
| Admin | `/admin/reports`, `/admin/audit_logs`, `/admin/profile` | GET/POST | Reports, audit trail, profile |
| Admin | `/admin/follow_ups` | GET | Follow-up appointments monitoring |
| Admin | `/admin/notifications` | GET | Notification & reminder monitoring |
| Admin | `/admin/hospital_info` | GET/POST | Public hospital information |
| Admin | `/admin/admins`, `/admin/admins/add`, `/admin/admins/<id>/toggle` | GET/POST | Administrator account management |
| Admin | `/admin/email_settings` | GET | Reminder / scheduler configuration |
| Shared | `/notifications` | GET / POST | View / mark notifications read |
| Shared | `/symptom_checker` | GET, POST | Symptom → department suggestion |

---

## 11. Authentication and Authorization

### Registration

- **Patients** — `/register` (email uniqueness, email regex validation, hashed password, optional DOB & emergency contact).
- **Doctors** — `/doctor/register` (adds qualification, license number, consultation fee, department, address; address is geocoded to lat/long via Nominatim).
- **Admins** — `/admin/register` is gated by a server-side secret key (`ADMIN_SECRET_KEY` env var). Without a matching key the registration is rejected; the created account always gets the `admin` role. Admins can also be created from the admin UI (`/admin/admins`).

### Login and sessions

- Each role has its own login page: patients use `/login`, doctors use `/doctor/login`, and admins use `/admin/login`.
- Every login page only authenticates its own role — e.g. valid doctor credentials submitted to `/login` are rejected with *"Doctors must log in using the Doctor Login page."* (and vice-versa), so a user can never land on another role's dashboard.
- A successful login sets `session['loggedin']`, `session['id']`, `session['email']`, `session['name']`, `session['user_type']`; `user_type` is always assigned server-side and is the source of truth for authorization.
- Sessions are Flask **cookie sessions** signed with `SECRET_KEY` — **no JWT is used**.
- Inactive accounts (`is_active = 0`) are rejected at login with a message.

### Logout

- `/logout` and `/api/logout` call `session.clear()`.

### Password handling

- Hashing: `werkzeug.security.generate_password_hash` / `check_password_hash` (pbkdf2/scrypt — passwords are never stored or logged in plain text).
- Minimum length of 6 characters enforced on password change forms.
- **Forgot password** — generates `secrets.token_urlsafe(16)`, stores it with a **1-hour expiry** on the Patient/Doctor row, emails a reset link (Brevo API), and validates token + expiry on `/reset_password/<token>`.

### Role-based access

| Decorator | Effect |
|---|---|
| `login_required` | Redirects to `/login` if not logged in |
| `patient_required` / `doctor_required` / `admin_required` | Redirects unless `session['user_type']` matches |
| `api_patient_required` / `api_doctor_required` / `api_admin_required` | Returns JSON `401` (not authenticated) or `403` (wrong role) |
| `api_login_required` | JSON `401` if no session |

Protected areas: every `/patient/*`, `/doctor/*`, and `/admin/*` page is wrapped in the matching decorator, and ownership checks ensure a doctor/patient can only act on their own appointments.

---

## 12. Appointment Workflow

```text
Patient
   ↓
Select Doctor  (search / department / nearby / symptom checker / chatbot)
   ↓
Check Availability  (DoctorSchedule → slots; free-text availability fallback)
   ↓
Select Date & Time  (live slot list, Sundays excluded)
   ↓
Book Appointment  → status = 'Pending'
   ↓   (DB unique slot_key prevents double booking)
In-app notification to Doctor + Patient, audit log entry
   ↓
Doctor Reviews  →  Approve (status = 'Approved', email to patient)
                ↘  Reject  (status = 'Rejected')
                ↘  Cancel  (status = 'Cancelled')
   ↓
Appointment Day  →  automated reminder email 30 minutes before
   ↓
Doctor Completes Appointment  →  status = 'Completed'
   ↓
Optional Follow-up (1–15 days) stored as Appointment.follow_up_date
   ↓
Follow-up reminder email sent on the follow-up date
   ↓
Patient sees updated status in dashboard / My Appointments / Notifications
```

**Follow-up flow:** when a doctor completes an appointment, they may enter a follow-up interval (0–15 days). The same appointment row stores `follow_up_date`; a background job (`send_follow_up_reminder`, running every minute) emails the patient on that date and marks `follow_up_reminder_sent`. The patient can then book a new appointment normally.

**Also enforced during booking:** doctor must be active and `Available`, date must not be in the past, requested time must belong to the doctor's availability for that date, the slot must be free, and the patient cannot hold a second live appointment with the same doctor on the same date.

---

## 13. Installation and Setup

### Prerequisites

| Tool | Purpose |
|---|---|
| **Python 3.9+** | Backend runtime |
| **pip** | Python package installation |
| **MySQL 8** (or a hosted MySQL instance) | Database |
| **Git** | Version control |
| *(optional)* **Node.js/npm** | Not required — the frontend is plain HTML/CSS/JS served by Flask |
| *(optional)* **Gunicorn** | Production server (already in `requirements.txt`) |

### Clone Repository

```bash
git clone https://github.com/KopparthiAkash05/hospital-system.git
cd hospital-system
```

*(If the URL changes, replace it with your own repository URL.)*

---

## 14. Backend Setup

Create and activate a virtual environment:

**Windows (PowerShell / CMD):**

```bash
python -m venv venv
venv\Scripts\activate
```

**macOS / Linux:**

```bash
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Key packages installed: `Flask`, `pymysql`, `Werkzeug`, `Flask-Mail`, `APScheduler`, `python-dotenv`, `gunicorn`, `requests`, `cryptography`.

Then create your environment file (see [Environment Variables](#17-environment-variables)) — the application **refuses to start** if `SECRET_KEY` or any `MYSQL_*` variable is missing.

---

## 15. Database Setup

1. **Create a MySQL database** (example name used by `schema.sql`):

   ```sql
   CREATE DATABASE IF NOT EXISTS hospital_db;
   ```

2. **Load the core schema** (Patient, Doctor, Admin, Appointment):

   ```bash
   mysql -u <user> -p hospital_db < schema.sql
   ```

3. **Load the feature migration** (Department, DoctorSchedule, Notification, AuditLog, new columns, seed departments, slot uniqueness index):

   ```bash
   mysql -u <user> -p hospital_db < migration_smart_hospital.sql
   ```

   The migration is written to be **idempotent** — every statement is guarded with `IF NOT EXISTS`/conditional checks, so re-running it is safe.

4. **Load the admin dashboard migration** (HospitalInfo table, `Admin.is_active`, and defensive Appointment reminder columns):

   ```bash
   mysql -u <user> -p hospital_db < migration_admin_dashboard.sql
   ```

   Also idempotent and safe to re-run.

5. *(Optional)* Import `freedb_xor9rxN1 (1).sql` — a phpMyAdmin dump of a working database including sample patients, doctors, and appointments — if you want demo data instead of an empty system.

6. **Create an admin account** — no admin is created by the SQL scripts with a working password, so insert one after loading the schema (hash generated with `werkzeug.security.generate_password_hash`):

   ```sql
   USE hospital_db;
   INSERT INTO Admin (name, email, password) VALUES ('Admin', 'admin@hospital.com', '<pbkdf2-hash>');
   ```

   Alternatively, register a patient in the UI and promote the row to the `Admin` table manually during development.

7. Point the app at the database with the `MYSQL_*` environment variables. There is **no ORM migration command** — schema changes are applied by running the SQL scripts directly.

---

## 16. Frontend / Client Setup

There is **no separate frontend build**. The UI is Jinja2 templates + static assets served by Flask:

```bash
# From the project root (same terminal as the backend)
python app.py
```

Then open **http://localhost:5000** (or the port set by the `PORT` environment variable).

Client-side scripts (`static/js/main.js`, `static/js/slot-refresh.js`) and CSS are served automatically at `/static/...` — no `npm install` or bundler is required. Chart.js and Font Awesome are loaded from CDNs, so an internet connection is needed for admin charts and icons.

---

## 17. Environment Variables

The app loads variables from `.env` and then from **`details.env`** in the project root (`python-dotenv`). `details.env` is listed in `.gitignore` and must **never** be committed.

Create `details.env` (or export the variables in your hosting provider's environment):

```env
# --- Required (the app raises RuntimeError if these are missing) ---
SECRET_KEY=<random-secret-key>
MYSQL_HOST=<mysql-host>
MYSQL_USER=<mysql-user>
MYSQL_PASSWORD=<mysql-password>
MYSQL_DB=<database-name>

# --- Email (password reset + appointment emails + reminders) ---
EMAIL_FROM=<sender-email-address>
BREVO_API_KEY=<brevo-api-key>
MAIL_USERNAME=<smtp-username>
MAIL_PASSWORD=<smtp-password>

# --- Optional / deployment ---
PORT=5000
DISABLE_SCHEDULER=0
RESEND_API_KEY=<unused-in-code>

# --- Admin registration secret (required only for /admin/register) ---
ADMIN_SECRET_KEY=<long-random-string>
```

| Variable | Required | Used for |
|---|---|---|
| `SECRET_KEY` | **Yes** | Signing Flask session cookies |
| `MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DB` | **Yes** | Database connection |
| `ADMIN_SECRET_KEY` | For admin signup | Secret key compared server-side by `/admin/register`; if unset, admin self-registration is disabled |
| `BREVO_API_KEY`, `EMAIL_FROM` | For email features | Brevo HTTP API (password reset, approval/completion emails, reminders) |
| `MAIL_USERNAME`, `MAIL_PASSWORD` | Optional | Flask-Mail / Gmail SMTP configuration |
| `PORT` | Optional | HTTP port (defaults to `5000`) |
| `DISABLE_SCHEDULER` | Optional | Set to `1` to stop APScheduler reminder jobs (useful for CLI/tools) |

> Never commit real keys or passwords. Use placeholders in any shared file.

---

## 18. Running the Application

**Terminal 1 — MySQL**

Start your MySQL server (local service, or use a hosted MySQL instance and skip this step).

**Terminal 2 — Backend (which also serves the frontend)**

```bash
cd hospital-system
venv\Scripts\activate          # Windows — or: source venv/bin/activate
python app.py                  # development server on http://localhost:5000
```

Production-style run (as defined in `Procfile`):

```bash
gunicorn app:app
```

**Browser**

Open `http://localhost:5000`, then:

- Patient → Register / Login
- Doctor → Doctor Register / Doctor Login
- Admin → Login with an account from the `Admin` table

Only one terminal is needed for the application itself (Flask renders pages and serves the JSON API). Background reminder jobs start automatically with the process unless `DISABLE_SCHEDULER=1`.

---

## 19. Testing

**Automated tests are not currently configured.** The repository contains no test framework, test files, or test scripts. Verification is done manually through the three role-based portals and the JSON API (for example `GET /api/health`).

---

## 20. Security

Implemented in the current codebase:

- **Password hashing** — Werkzeug `generate_password_hash` / `check_password_hash` (never plain-text storage).
- **Session-based authentication** — signed cookie sessions with `SECRET_KEY`; session cleared on logout.
- **Role-based authorization** — server-side decorators on every protected page and JSON endpoint (401/403 JSON for API clients).
- **Ownership checks** — doctors and patients can only modify their own appointments; admin pages are admin-only.
- **SQL injection prevention** — parameterized queries (`%s` placeholders) everywhere; identifiers are never interpolated from user input.
- **Environment variables** — secrets (`SECRET_KEY`, DB credentials, API keys) are read from the environment / gitignored `details.env`, never hard-coded as fallbacks.
- **Credential exposure control** — the public `/api/doctors` endpoint selects an explicit column whitelist so password hashes and reset tokens are never returned.
- **Account deactivation** — admins can disable patient/doctor accounts; deactivated users are blocked at login.
- **Database-level double-booking protection** — unique generated `slot_key` index on live appointments.
- **Password reset safety** — random tokens with a 1-hour expiry; generic response when no account matches the email.
- **Input validation** — email format checks, required-field checks, date/time validation, password length and confirmation checks.
- **Scheduler safety** — a single scheduler per process to avoid duplicate reminder emails; log streams forced to UTF-8 to prevent job crashes.

Not implemented (do not assume): CSRF tokens, rate limiting, HTTP security headers, JWT/token API auth, two-factor authentication.

---

## 21. Future Enhancements

Clearly labelled as **Planned / Future Enhancement** (not present today):

- Real-time notifications (WebSockets / server-sent events) instead of page-refresh in-app notifications
- Token-based (JWT) authentication for the JSON API and a dedicated mobile client
- Doctor *application approval* workflow with pending/approved states
- Patient favourite doctors list
- Appointment status-history table with full timeline
- Cancellation email wired into all cancellation paths (helper exists but is not called)
- PDF/CSV report export and richer analytics (currently revenue is a fixed ₹500-per-completed-appointment estimate)
- Online payment integration for consultation fees
- Cloud deployment pipeline with CI/CD and automated tests
- NLP-based chatbot upgrade beyond the current rule-based decision tree
- Multi-language support and accessibility improvements

---

## 22. Project Limitations

Known limitations of the current implementation:

- **Single-file backend** — all backend logic lives in `app.py` (~5,000 lines); there is no module/package split, service layer, or ORM.
- **No automated tests or CI configuration** in the repository.
- **Admin self-registration is secret-gated** — `/admin/register` requires the `ADMIN_SECRET_KEY` environment variable to match; without it (or with a wrong key) registration is rejected. Admins can also create accounts from the admin UI (`/admin/admins`).
- **Doctor registration is immediate** — there is no pending-approval queue; the admin's control is activation/deactivation of accounts.
- **Notifications are not real-time** — in-app notifications update on page load; emails depend on a valid third-party API key (`BREVO_API_KEY`), otherwise reminder/reset emails silently fail (password reset falls back to showing the link on screen).
- **The `/api/*` endpoints use browser sessions**, not tokens — they are suited to same-origin clients rather than third-party mobile apps.
- **Some `requirements.txt` packages are unused** by the running code (`Flask-MySQLdb`, `sendgrid`, `resend`), and one helper named `send_email_via_sendgrid` actually calls the Brevo API.
- **Geolocation columns** (`Patient.latitude`, `Patient.longitude`) are required by Nearby Doctors but are not created by the supplied SQL scripts (see the note in [Database Architecture](#9-database-architecture)).
- **No license file** and **no screenshots** are included in the repository.

---

## 23. Screenshots

No screenshots are currently included in the repository. Suggested captures if you add them later: landing page, patient dashboard, booking with live slots, doctor appointment queue, admin analytics dashboard, audit logs.

---

## 24. License

No license file is present in this repository. Add one (for example MIT) if you intend to distribute the project.

---

## 25. Author / Project Information

```text
Repository : https://github.com/KopparthiAkash05/hospital-system
Developed as an academic project.
```
