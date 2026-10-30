# Claims Surveillance — API Contract (hisi app)

| | |
|---|---|
| **Feature** | `claims-surveillance` — mandate → auditable session → location-verified surveillance |
| **App** | `hisi` (module `Hisi`) |
| **Status** | CONTRACT — implementation-ready; reflects all 2026-10-30 decisions (two roles, local rebuild `HISI Health Facility` / `HISI Professional Record`, no claim doctypes, surveilled-only display, `surveiled_refs` + `surveiled_targets` reports; payer registry `HISI Payer Organization` — role + doctype, 2026-10-30; Q1/Q3/Q4/Q5 locked); envelope mechanics verified against code 2026-10-30 |
| **Date** | 2026-10-30 |
| **Source spec** | `claims_surveillance_feature_spec.md` — §8 API design, §9 geofence, §10 audit, §12 settings; epics: `claims_surveillance_epics.md` (S0.1–S5.4) |
| **Related** | `patient_flow_api_spec.md` v1.2 — `surveil_patient` is a thin wrapper over it (same response shape) |

---

## 1. Conventions (house rules — no exceptions)

- **Base path** `/api/method/hisi.api.surveillance/<function>` — the exact Frappe dotted route (`hisi.api.surveillance` is the package public surface, re-exporting the per-module functions; the feature spec's slash form `/api/method/hisi/api/surveillance/…` is prose shorthand). New package `hisi/api/surveillance/` — `mandate.py`, `session.py`, `location.py`, `scope.py`, `audit.py`, `api.py`, `errors.py`.
- **Auth** — Frappe session/token on every endpoint; **no `allow_guest`**. Identity is always the Frappe session — the mandate QR code selects *which mandate*, never identity (MFA email OTP applies as on every HISI login).
- **Single response layer** — every function is `@frappe.whitelist()` + wrapped in `@api_endpoint` (imported from `hisi/api/response_handler.py`); all responses go through `APIResponse`:
  - success: `{"status": "success", "data": {…}}` — via `APIResponse.success(data=…)`
  - error: `{"status": "error", "code": "…", "message": "…", "errors": []}` (HTTP status per code table); `frappe.PermissionError` → 403 `FORBIDDEN`, and any other exception → `frappe.log_error` + 500 `INTERNAL_ERROR` — the `@api_endpoint` wrapper is the **only** exception handler, and it swallows everything it does not name (verified against `response_handler.py`, 2026-10-30)
- **Wire format** — request arguments and `data` fields are **snake_case** (matches the Frappe wire convention of every existing hisi API and the `patient_flow` pass-through); the frontend normalizes in `lib/hisi-service.ts` as it already does. Nested structures (`scope_lines`, `location`, `device`) arrive as native objects on JSON POST; the JSON-string form is accepted and parsed identically (form-encoded tolerance, same as `patient_flow`). The wire parameter `session_id` maps to `HISI Surveillance Session.session_no`.
- **Error codes** — standard set from `response_handler`: `BAD_REQUEST` (400), `UNAUTHORIZED` (401), `FORBIDDEN` (403), `NOT_FOUND` (404), `CONFLICT` (409), `UPSTREAM_ERROR` (400), `INTERNAL_ERROR` (500). The surveillance-specific codes below are **thin `APIResponse` subclasses** in `hisi/api/surveillance/errors.py` — same `respond()` path, single response layer intact:

  | code | HTTP | Raised when |
  |---|---|---|
  | `MANDATE_INVALID` | 400 | code unknown / expired / revoked / locked — **no PII in the message** |
  | `LOCATION_CHECK_FAILED` | 400 | strict policy + failed check-in; message carries the reason (`out_of_range` / `spoofed` / `stale` / `low_accuracy`) and the distance in metres |
  | `MANDATE_REVOKED` | 403 | mandate or its session was revoked mid-flow |

- **Roles (two, v1)** — `HISI Insurance Provider` (payer side: the caller is a member of a `HISI Payer Organization` row, row-scoped via the `is_payer_member` hook in `hisi/permissions.py` — the payer registry is a dedicated doctype, separate from the SHA-pipeline `HISI Vendor Organization`) and `HISI Insurance Surveillance` (no doctype permissions at all — API only, session-gated). Existing platform roles (System Manager / HISI Reviewer) are full.
- **`require_session` gate** — every in-session endpoint (`get_session`, `surveil_patient`, `get_facility`, `list_practitioners`, `recheck_location`, `end_session`) resolves the session **first**: status `active`, within TTL, idle not exceeded, `session.user == caller`. Lazy expiry: a call that finds the session lapsed marks it `expired`, writes `session_expired`, and returns 403 — no scheduler dependency.
- **Audit** — one `HISI Surveillance Event` per call (success, denial, or error), with the two documented multi-row exceptions: `start_session` success (`checkin_verified` **then** `session_started`) and `surveil_patient` (one per executed step, up to 4). The officer's own `get_session` state read writes no event (no catalog type — §3.4). The Event doctype is insert-only (no delete permission for any role). Event writes are **best-effort**: insert failure → `frappe.log_error` + session `audit_degraded` flag, the call still returns (spec §10.2).
- **Rate limiting** — `frappe.rate_limiter` on every endpoint; tightest on `verify_mandate` (brute-force protection). Independent of rate limits, a **server-side 5-strike lockout** counter per code (DB/cache-backed, survives refresh) → `mandate_verify_locked`.
- **429 stacking (verified against bench Frappe source, 2026-10-30)** — `frappe.RateLimitExceededError` is a plain `frappe.ValidationError` with `http_status_code = 429`, **not** an `APIResponse`. It surfaces as a native 429 (plain `Too Many Requests` body — the only non-envelope response in this contract) only when `@frappe.rate_limiter.rate_limit` is stacked **outside** `@api_endpoint`; reversed stacking gets swallowed into a 500 `INTERNAL_ERROR`. Pinned stacking for every endpoint:

  ```python
  @frappe.whitelist(methods=["POST"])
  @frappe.rate_limiter.rate_limit(limit=…, seconds=…)
  @api_endpoint
  def issue_mandate(…): …
  ```

- **No tunable constant in code** — TTL, idle, radius, accuracy gate, lockout count, retention all come from the HISI Settings Surveillance tab (spec §12).
- **PII posture** — list views and audit rows carry masked identifiers (`target_ref_masked`); full identifiers live only in event `details` (privileged read) and in the session's `surveiled_refs` report.

---

## 2. Shared shapes

**Scope line**
```json
{ "line_type": "patient", "patient_identifier": "36637305", "visit_id": null, "note": "…" }
```
`line_type` ∈ `patient` | `visit` (claims are the dispatch context on the mandate `reason`, never a data surface).

**Mandate**
```json
{
  "mandate_no": "SURV-8F3A2C", "payer_org": "ACME-ORG-1", "payer_org_name": "Acme Assurance",
  "dispatched_by": "dispatcher@acme.example", "assigned_officer": "officer@acme.example",
  "facility": "HFI-000123", "facility_name": "Nairobi East Clinic",
  "reason": "Suspicion on claim CLM-2026-0441 — check visit of 12 Oct",
  "scope_lines": [ { "line_type": "patient", "patient_identifier": "36637305", "visit_id": null, "note": null } ],
  "valid_from": "2026-10-30T08:00:00", "expires_at": "2026-10-31T08:00:00",
  "short_code": "8F3A2C91", "qr_url": "https://<site>/api/method/hisi.api.surveillance/verify_mandate?code=8F3A2C91",
  "radius_km": null, "location_policy": "strict",
  "status": "dispatched", "signature": "9f2c…", "created_on": "2026-10-30T08:00:00"
}
```
`status` ∈ `dispatched` / `active` / `completed` / `revoked` / `expired`. `short_code` = `secrets.token_hex(4)` uppercased (8 chars, unique); returned in full **only** by `issue_mandate` to the issuer; `qr_url` deep-links `verify_mandate?code=…` (the officer UI's scan handler maps it to the app verify screen). `signature` = HMAC-SHA256 of `(payer_org, facility, assigned_officer, scope hash, valid_from, expires_at, short_code)` with the site key.

**Session**
```json
{
  "session_no": "SS-291029A", "mandate": "SURV-8F3A2C", "user": "officer@acme.example",
  "started_at": "2026-10-30T10:14:03", "ends_at": "2026-10-30T14:14:03", "last_activity": "2026-10-30T10:14:03",
  "location_status": "verified", "checkin_distance_m": 182.4, "checkin_accuracy_m": 18,
  "checkin_location": { "lat": -1.2833, "lng": 36.8219, "ts": "2026-10-30T10:14:03Z", "is_mock": false, "platform": "web" },
  "device_meta": { "platform": "web", "app_version": "1.4.2", "ip": "102.98.0.1", "user_agent": "…" },
  "flags": [], "surveiled_refs": [ { "type": "patient", "ref": "CR247…58", "at": "2026-10-30T10:21:44" } ],
  "status": "active", "summary": null
}
```
`location_status` ∈ `verified` / `out_of_range` / `spoofed` / `unreported`; `status` ∈ `active` / `completed` / `expired` / `revoked`. `ends_at = started_at + surveillance_session_ttl_minutes`. `surveiled_refs` is **append-only** per session — patient / claim / visit identifiers (from scope) and practitioner refs, one entry per in-scope use.

**Event**
```json
{
  "event_type": "patient_viewed", "user": "officer@acme.example",
  "target_type": "patient", "target_ref_masked": "CR247…58",
  "details": { "identifier": "36637305", "steps": ["patient", "eligibility"] },
  "ip": "102.98.0.1", "location": null, "outcome": "ok", "duration_ms": 41, "created_on": "2026-10-30T10:21:44"
}
```
`target_type` ∈ `patient` / `visit` / `facility` / `practitioner` / `record` / `mandate` / `session`; `outcome` ∈ `ok` / `denied` / `error` / `flagged`. `session` (nullable — null for mandate-level events and pre-gate failures) is on the row, omitted from the list shape above.

**Facility view** (from `HISI Health Facility`)
```json
{
  "facility": "HFI-000123", "name": "Nairobi East Clinic", "facility_code": "KE-NEC-001",
  "facility_fid": "FID-88912", "county": "Nairobi", "level": "Hospital", "operational_status": "Active",
  "license": "Licence 4471/2024", "coordinates": { "lat": -1.2833, "lng": 36.8219 }
}
```
Geofence internals (`surveillance_radius_km`) are **never returned** by any API.

**Practitioner view** (from `HISI Professional Record`)
```json
{ "record_id": "PR-10023", "name": "Dr. A. Wanjiku", "cadre": "Medical Officer", "regulator": "KMPDC", "registration_number": "KMPDC/4471", "facility": "HFI-000123" }
```

---

## 3. Endpoints

### 3.1 `issue_mandate` — POST, Dispatcher

Issue a surveillance mandate for the caller's payer org.

**Request**
```json
{
  "facility": "HFI-000123",
  "assigned_officer": "officer@acme.example",
  "reason": "Suspicion on claim CLM-2026-0441 (reqd, ≤500)",
  "scope_lines": [
    { "line_type": "patient", "patient_identifier": "36637305", "note": null },
    { "line_type": "visit", "visit_id": "V-20261012-008", "note": "12 Oct visit" }
  ],
  "valid_from": "2026-10-30T08:00:00",
  "expires_at": "2026-10-31T08:00:00",
  "radius_km": null,
  "location_policy": "strict",
  "facility_detail": null
}
```

**Response `data`** — the full **Mandate** shape (§2), status `dispatched`, with `short_code` + `qr_url` + `signature`.

**Behaviour & side effects**
- `payer_org` = the caller's `HISI Payer Organization` via `is_payer_member` (payer-registry membership — never a request parameter).
- `facility` must exist or be created as a `HISI Health Facility` row (the surveilled-target registry gains the row on first dispatch). For a **new** facility the caller also supplies `facility_detail` = `{ name, facility_code, county, level, operational_status, license, lat, lng }`; `lat` / `lng` are **required** for creation — a facility without coordinates cannot be geofenced (missing → `400 BAD_REQUEST`).
- `assigned_officer` must be a valid User; `scope_lines` ≥ 1; `expires_at` > `valid_from`.
- `radius_km` optional (else Q4 order: facility field → settings default); `location_policy` optional (default = settings `surveillance_strict_location` ⇒ `strict`).

**Errors** — `BAD_REQUEST` (invalid officer, empty scope, inverted window, bad policy, missing `facility_detail` or coordinates for a new facility); `FORBIDDEN` (not a Dispatcher / cross-payer).
**Event** — `mandate_issued` (target_type `mandate`).

---

### 3.2 `verify_mandate` — GET or POST, Officer (QR deep link lands on GET)

Validate a code without patient PII.

**Request** — `code` (8 chars): GET query param `?code=8F3A2C91` or POST body `{"code": "8F3A2C91"}`.

**Response `data`** (success, code valid and assigned to the caller)
```json
{
  "valid": true, "mandate_no": "SURV-8F3A2C",
  "facility": { "name": "Nairobi East Clinic" },
  "scope_counts": { "patient": 1, "visit": 1 },
  "valid_from": "2026-10-30T08:00:00", "expires_at": "2026-10-31T08:00:00",
  "assigned_to_me": true
}
```
No identifiers, no patient data — facility **name** only.

**Failure** — any of unknown / expired / revoked / not-mine / locked → `400 MANDATE_INVALID` (identical message for all, no PII). Each failure increments the code's strike counter; **5 consecutive failures** → code locked, further attempts also `MANDATE_INVALID`.
**Events** — `mandate_verified` / `mandate_verify_failed` / `mandate_verify_locked` (session-null where identity is unknown).
**Notes** — tightest rate limit + lockout live here.

---

### 3.3 `start_session` — POST, Officer (the check-in)

Verified mandate + single-point GPS check-in → geofence decision server-side → session.

**Request**
```json
{
  "mandate_code": "8F3A2C91",
  "location": { "lat": -1.2833, "lng": 36.8219, "accuracy_m": 18, "ts": "2026-10-30T10:14:03Z", "is_mock": false, "platform": "web" },
  "device": { "app_version": "1.4.2", "user_agent": "…" }
}
```

**Server decision** (spec §9.2 matrix)
- Re-validate the mandate (stale-code defence) → `MANDATE_INVALID` on failure.
- Geofence: centre `HISI Health Facility.latitude/longitude`; radius = `mandate.radius_km` → `HISI Health Facility.surveillance_radius_km` → settings `surveillance_default_radius_km`; haversine distance in metres, **recorded in every outcome**; accuracy gate `surveillance_max_accuracy_m` (200), ts freshness ≤ 2 min, client `is_mock` input, `platform: web`.
- `strict` + failed check-in → `400 LOCATION_CHECK_FAILED`, **no session created** (event `checkin_failed` with the distance).
- Passed (or `flag` mode: session created + flagged) → session `active`, TTL from settings, mandate flips `dispatched → active`.

**Response `data`** — full **Session** shape (§2), status `active`, plus `mandate_no`.
**Errors** — `UNAUTHORIZED` (not the assigned officer); `MANDATE_INVALID`; `LOCATION_CHECK_FAILED`; `BAD_REQUEST` (missing/invalid location payload); `MANDATE_REVOKED`.
**Events** — `checkin_verified` + `session_started` (or `checkin_failed`).

---

### 3.4 `get_session` — GET, Officer

**Request** — `session_id` (query/body).

**Response `data`**
```json
{ "session": { "…full Session shape…": "" }, "time_remaining_sec": 13204,
  "scope_lines": [ { "line_type": "patient", "patient_ref_masked": "CR247…58", "note": null } ] }
```
Scope lines **masked** (the officer sees unmasked identifiers only inside scoped `surveil_patient` calls).
**Errors** — `NOT_FOUND` (no such / no active session); `FORBIDDEN` (not the session's officer; inactive session → 403 with the current status in the message); lazy `session_expired` on a lapsed session.
**Events** — none on success: the officer's own state read has no catalog type (it refreshes `last_activity`; privileged reviewer reads do write — `sessions_listed` / `audit_viewed`, §4). Gate failure writes the usual `session_expired` / `auth_denied`.

---

### 3.5 `surveil_patient` — POST, Officer

Thin wrapper over the implemented `hisi.api.patient.patient_flow` — **response `data` shape is identical to `patient_flow` v1.2 (§4 of that spec)**; nothing re-specified here.

**Request**
```json
{
  "session_id": "SS-291029A",
  "steps": ["patient", "eligibility", "consent", "records"],
  "filters": { "identification_type": "National ID", "identification_number": "36637305" }
}
```
(`cr_id` / `visit_id` filters pass through per the `patient_flow` contract.)

**Scope** — the identifier must resolve to a mandate scope line; out-of-scope → `403 FORBIDDEN` + `scope_denied` event. **Per-step events** — `patient_viewed` / `eligibility_viewed` / `consent_viewed` / `records_viewed` with step names in `details`. Each identifier used is appended to the session's `surveiled_refs`.
**Errors** — `FORBIDDEN` (out of scope / wrong role / inactive session); `UPSTREAM_ERROR` (DHA / CMP / HealthLake failure — propagated, + `error` event); `NOT_FOUND` (identifier unresolvable).

---

### 3.6 `get_facility` — GET, Officer

**Request** — `session_id`.

**Response `data`** — the **Facility view** (§2) for the **mandate's facility** (the only facility this session may see). Licence, operational status, level, coordinates — never the radius or geofence internals.
**Errors** — `FORBIDDEN` (inactive session); `NOT_FOUND` (facility row missing — should not happen, created at issue).
**Events** — `facility_viewed`.

---

### 3.7 `list_practitioners` — GET, Officer

**Request** — `session_id`.

**Response `data`**
```json
{ "facility": "HFI-000123", "practitioners": [ { "record_id": "PR-10023", "name": "Dr. A. Wanjiku", "cadre": "Medical Officer", "regulator": "KMPDC", "registration_number": "KMPDC/4471", "facility": "HFI-000123" } ] }
```
**Only** `HISI Professional Record` rows linked to the surveilled facility — a facility with no rows returns `practitioners: []` (200, not an error). Viewed practitioner refs are appended to `surveiled_refs`.
**Errors** — `FORBIDDEN` (inactive session).
**Events** — `practitioner_viewed`.

---

### 3.8 `recheck_location` — POST, Officer (optional mid-session re-check)

**Request** — same `location` + `device` payload as `start_session`, plus `session_id`.

**Server** — same geofence decision as check-in, plus the **impossible-jump check**: implied speed between this fix and the check-in fix > 5 km/h over the elapsed time → flag the session (`flags += ["impossible_move"]`, outcome `flagged`).
**Response `data`**
```json
{ "location_status": "verified", "checkin_distance_m": 191.0, "checkin_accuracy_m": 22, "flagged": [] }
```
**Errors** — `FORBIDDEN` (inactive session); `BAD_REQUEST` (bad payload; recheck disabled by `surveillance_recheck_enabled = 0`).
**Events** — `session_recheck` (distance + status in `details`; `flagged` outcome when the jump check trips).

---

### 3.9 `end_session` — POST, Officer

**Request**
```json
{ "session_id": "SS-291029A", "summary": "Two staff interviewed; visit records match claim.", "location": null }
```
`location` optional — a final check-in run through the same matrix (outcome recorded, does not block completion).

**Response `data`** — `{ "session_no": "SS-291029A", "status": "completed", "summary": "…", "ended_at": "…", "surveiled_refs": [ … ] }`
**Errors** — `CONFLICT` (409) when the session is already `completed` / `revoked` / `expired`; `FORBIDDEN` (not the session's officer).
**Events** — `session_ended` (and `checkin_verified`/`checkin_failed` when a final check-in ran).

---

### 3.10 `list_sessions` — GET, Dispatcher (payer-scoped read surface)

**Request**
```json
{ "page": 1, "page_size": 20, "status": null, "location_status": null }
```
`page_size` default 20, max 100; `status` / `location_status` optional filters.

**Response `data`**
```json
{
  "sessions": [
    { "session_no": "SS-291029A", "mandate_no": "SURV-8F3A2C", "officer": "officer@acme.example",
      "facility_name": "Nairobi East Clinic", "status": "active", "location_status": "verified",
      "checkin_distance_m": 182.4, "flags": [], "started_at": "…", "ends_at": "…" }
  ],
  "page": 1, "page_size": 20, "total": 37, "total_pages": 2
}
```
Payer-scoped via the `is_payer_member` hook (membership of `HISI Payer Organization`); refs masked; flagged rows (`location_status != verified` or `flags` non-empty) carry their flags so the review UI can highlight them.
**Errors** — `BAD_REQUEST` (bad page); cross-payer rows simply never appear (scope, not error).
**Events** — `sessions_listed` (session-null event, target_type `session`, counts in `details`).

---

### 3.11 `get_session_audit` — GET, Dispatcher (the exportable record)

**Request** — `session_id`.

**Response `data`**
```json
{
  "session": { "…full Session shape incl. surveiled_refs…": "" },
  "events": [ { "…full Event shape…": "", "details": { "full identifier + context — privileged read" } } ],
  "surveiled_targets": { "facility": { "…Facility view…": "" }, "practitioners": [ { "…Practitioner view…": "" } ] },
  "export": { "format": "json", "generated_at": "2026-10-30T12:00:00" }
}
```
The session (including **`surveiled_refs`** — the patient / claim / visit identifiers and practitioner refs actually used) + the complete ordered event list. The `surveiled_targets` block is the report's explicit statement of the surveilled-only registries (§2.4): the facility and practitioners the mandate covered — nothing else exists in those registries. CSV/PDF rendering of the same envelope is v1.1.
**Errors** — `NOT_FOUND`; `FORBIDDEN` (not the payer's session / platform read denied).
**Events** — `audit_viewed`.

---

### 3.12 `revoke_mandate` — POST, Dispatcher or platform admin

**Request** — `{ "mandate_no": "SURV-8F3A2C" }`

**Behaviour** — mandate → `revoked`; **every active session for it → `revoked` immediately**; every subsequent in-session call fails with `403 MANDATE_REVOKED`.
**Response `data`** — `{ "mandate_no": "SURV-8F3A2C", "status": "revoked", "revoked_sessions": 1 }`
**Errors** — `NOT_FOUND`; `FORBIDDEN` (cross-payer Dispatcher); `CONFLICT` (already revoked/expired — idempotent re-revocation).
**Events** — `mandate_revoked` + one `session_revoked` per killed session (outcome `denied`; contract extension, §4).

---

## 4. Event catalog (contract view — superset of spec §10.1)

`mandate_issued`, `mandate_verified`, `mandate_verify_failed`, `mandate_verify_locked`, `mandate_revoked`, `checkin_verified`, `checkin_failed`, `session_started`, `session_expired`, `session_ended`, `session_recheck`, `patient_viewed`, `eligibility_viewed`, `consent_viewed`, `records_viewed`, `facility_viewed`, `practitioner_viewed`, `scope_denied`, `auth_denied`, `error` — **plus the events added by this contract**: `sessions_listed` and `audit_viewed` (read surface, for the reviewer endpoints) and `session_revoked` (per session killed by `revoke_mandate`); fold all three into spec §10.1 at implementation so the S3.5 audit-completeness matrix stays green.

Invariants (spec §10.2, restated): one event per API call — with the multi-row exceptions listed in §1 (`start_session`, `surveil_patient`) and the `get_session` read exemption (§3.4); pre-gate failures that cannot resolve a session write a session-null `auth_denied`/`error` event; event insert failure → `audit_degraded` + `log_error`, never a raised error; insert-only (no role holds write/delete); 6-year retention posture (`surveillance_retention_days`, default 2192).

## 5. Settings (HISI Settings — Surveillance tab, spec §12)

| fieldname | default |
|---|---|
| `surveillance_session_ttl_minutes` | 240 |
| `surveillance_idle_timeout_minutes` | 30 |
| `surveillance_default_radius_km` | 1.0 |
| `surveillance_max_accuracy_m` | 200 |
| `surveillance_strict_location` | 1 |
| `surveillance_recheck_enabled` | 1 |
| `surveillance_verify_lockout_attempts` | 5 |
| `surveillance_retention_days` | 2192 |

Unset required values fail fast with a named `BAD_REQUEST` — no silent defaults in code.

## 6. Permissions & hooks (cross-ref, spec §8.6)

- `hisi/permissions.py` gains `has_permission` + `get_permission_query_conditions` for the four surveillance doctypes: Dispatcher/`HISI Health Facility`/`HISI Professional Record` payer-scoped via `is_payer_member` (`HISI Payer Organization Member` — separate from the vendor-side `HISI Organization Member`); officer scoped to `doc.user == frappe.session.user`; platform roles full. The two local rebuild doctypes: System Manager full, Dispatcher read (payer-scoped), **no officer doctype access**.
- `User Permission` is **not** used (static per-user grants cannot express session scope) — `require_session` is the enforcement point; the S3.5 test asserts one event per call on every endpoint (per the §1 exception list).

## 7. Frontend operation allow-list (hisi_app — epic S4.1)

`lib/server/operations.ts` + zod in `lib/operations-contract.ts`; browser never calls Frappe directly:

| operation | HTTP | upstream |
|---|---|---|
| `surveillance.issue-mandate` | POST | `issue_mandate` |
| `surveillance.verify-mandate` | GET/POST | `verify_mandate` |
| `surveillance.start-session` | POST | `start_session` |
| `surveillance.get-session` | GET | `get_session` |
| `surveillance.surveil-patient` | POST | `surveil_patient` |
| `surveillance.get-facility` | GET | `get_facility` |
| `surveillance.list-practitioners` | GET | `list_practitioners` |
| `surveillance.recheck-location` | POST | `recheck_location` |
| `surveillance.end-session` | POST | `end_session` |
| `surveillance.list-sessions` | GET | `list_sessions` |
| `surveillance.get-session-audit` | GET | `get_session_audit` |
| `surveillance.revoke-mandate` | POST | `revoke_mandate` |

## 8. Explicitly out of this contract

- `patient_flow` internals (DHA JWT / SigV4 / CMP UAT / HealthLake) — v1.2 spec governs; only its response shape is inherited by `surveil_patient`.
- Claim endpoints — **none** (2026-10-30 decision): claims are dispatch context / audit reference only (`reason` + `surveiled_refs`).
- CSV/PDF export of the audit record (v1.1), external payer dispatch API / M2M keys (v1.1), native anti-spoofing wrapper (v2).

---
*Companion to `claims_surveillance_feature_spec.md` and `claims_surveillance_epics.md`. Story IDs S3.2–S3.4/S4.1 map 1:1 to §3.1–§3.12 + §7. Envelope mechanics (success/error shapes, the seven existing sentinels, wrapper swallow behaviour, 429 stacking) verified against `hisi/api/response_handler.py` and the bench Frappe source (`frappe/rate_limiter.py`, `frappe/app.py`) on 2026-10-30.*
