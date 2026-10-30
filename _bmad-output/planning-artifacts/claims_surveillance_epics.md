# Claims Surveillance — Epics & Stories (BMAD plan phase)

| | |
|---|---|
| **Source spec** | `claims_surveillance_feature_spec.md` — DRAFT; Q1/Q3/Q4/Q5 locked 2026-10-30 (in-platform dispatch, strict default, dedicated `surveillance_radius_km`, mobile web v1); **local rebuild locked 2026-10-30** (facility/practitioner doctypes built inside the hisi app — no claim doctypes; no global_mediator / healthpro reliance); **payer registry locked 2026-10-30** (`HISI Payer Organization` doctype + `HISI Insurance Provider` role — spec §7.5, payer = role + doctype); Q10 + remainder pending |
| **App** | `hisi` (module `Hisi`) + `hisi_app` (Next.js, Epic 4) |
| **Date** | 2026-10-30 |
| **Status** | Ready for implementation after Q10 confirmation (see R5); zero code exists for this feature yet |
| **Conventions** | All spec §-references point at the feature spec. House rules (§17) apply to every story: single response layer (`response_handler.py` + `api_endpoint`), no docstrings, no private names, one blank line between functions, pydantic at boundaries, no tunable constants in code (HISI Settings), responses only via `APIResponse`. |

**Execution order:** Epic 0 first (doctype + settings + audit writer — everything depends on it). Epic 1 before 2 (verify → session). Epic 3 after 2 (scope gate + surveillance endpoints). Epic 4 (UI) can start at S4.1 in parallel once the corresponding Epic 1–3 endpoints land per-operation. Epic 5 last.

**Definition of done (every story):** feature code + tests per §15, `ruff check` clean (indentation matching neighbouring hisi files), no secret/URL constants, responses only via `response_handler.py`, one `HISI Surveillance Event` per call where the spec mandates (§10.2), and the existing suite (47 green) stays green.

---

## Epic 0 — Foundation (doctypes, settings, roles, audit writer)

*Goal: the data model installs cleanly and every later story builds on the append-only event writer and the payer-scoped permission hooks.*

### S0.1 Roles + doctypes (incl. the payer registry)
- **Deliver:** roles `HISI Insurance Provider` (who dispatches) / `HISI Insurance Surveillance` (who goes to surveillance) — the two v1 surveillance roles (created via install/seed, the `seed.py` pattern); the review surface sits on the Dispatcher, a read-only `Reviewer` role is a later option, not v1; doctypes `HISI Surveillance Mandate` (+ child `HISI Surveillance Scope Line`), `HISI Surveillance Session`, `HISI Surveillance Event` with all §7 fields, plus `HISI Payer Organization` (+ child `HISI Payer Organization Member`, §7.5 — the payer is the `HISI Insurance Provider` role **and** this registry doctype, separate from the SHA-pipeline `HISI Vendor Organization`) and the `is_payer_member` helper in `hisi/utils.py`; permission matrix per §8.6 L1 — including **no delete on Event for any role** and **no officer doctype access** to mandate/session.
- **Accept:** `bench migrate` on `hisi-local` clean; Event doctype exposes no delete path (permission query + direct `frappe.delete_doc` as officer → PermissionError); officer has zero Desk access to mandates; a payer org + member row installs and `is_payer_member` resolves membership.
- **Deps:** none. **Size:** M.

### S0.2 HISI Settings — Surveillance tab
- **Deliver:** `hisi_settings.json` gains the §12 tab + 9 fields with defaults (TTL 240, idle 30, radius 1.0, accuracy 200, strict 1, recheck 1, lockout 5, retention 2192).
- **Accept:** fields persist across bench restart; unset-required fail-fast paths return the named `BAD_REQUEST` (unit probe per field).
- **Deps:** none (parallel-safe with S0.1). **Size:** S.

### S0.3 Local rebuild — `HISI Health Facility` / `HISI Professional Record` (no claim doctypes)
- **Deliver:** two new standalone doctypes in the hisi app (local rebuild, spec §2.4 + Q13 — **no cross-app dependency, no claim doctypes**): `HISI Health Facility` (name, facility code/fid, county, level, operational status, license; `latitude`/`longitude` Float precision 7; `surveillance_radius_km` Float — the Q4 field lives here); `HISI Professional Record` (name, cadre, regulator, registration number, `facility` Link → `HISI Health Facility`). **Surveilled-only semantics:** rows exist only for facilities named in a mandate / practitioners of surveilled facilities; the UI surfaces nothing else; the surveilled-only property is also captured in the report — the `get_session_audit` export carries a `surveiled_targets` block (mandate facility + its practitioners). Permissions: System Manager full; Dispatcher read (payer-scoped per S0.5); **no officer doctype access** (API only, §8.6 L1).
- **Accept:** `bench migrate` clean; radius field present (empty sites read `None` → falls through the §9.1 order); grep assertion: no `healthpro`/`global_mediator` doctype name or module reference, and no claim doctype, anywhere in `hisi/api/surveillance/`.
- **Deps:** none. **Size:** S.

### S0.4 Audit writer
- **Deliver:** `hisi/api/surveillance/audit.py::log_event(session, user, event_type, target_type, target_ref, details, outcome, …)` — the single event writer: masked `target_ref_masked`, `ip`, location JSON for check-in events, `duration_ms`; **best-effort semantics** per §10.2 (insert failure → `frappe.log_error` + session `audit_degraded` flag, never raise into the caller).
- **Accept:** every event row carries who/what/when/where/outcome; a simulated insert failure produces the `audit_degraded` flag + an `Error Log` row and the API call still returns; append-only assertion (15.2 seed).
- **Deps:** S0.1. **Size:** M.

### S0.5 Payer-scoped permission hooks
- **Deliver:** `hisi/permissions.py` extension (or sibling module, same pattern): `has_permission` + `get_permission_query_conditions` for the four doctypes — Dispatcher scoped to `payer_org` via `is_payer_member` (`HISI Payer Organization Member` — separate from the vendor-side `HISI Organization Member`); officer scoped to `doc.user == frappe.session.user`; platform roles full.
- **Accept:** cross-payer list queries return 0 rows; officer sees only own sessions; matrix tests per §15 (S5.2).
- **Deps:** S0.1. **Size:** M.

---

## Epic 1 — Mandate (dispatch + verification + revocation)

*Goal: the insurer's dispatch is provable — a mandate exists, names an officer, a facility, a scope, a window, and an unguessable code.*

### S1.1 `issue_mandate`
- **Deliver:** Dispatcher endpoint (§8.1): facility + ≥1 scope line + officer + window (+ optional radius/policy); generates `short_code` = `secrets.token_hex(4)` uppercased, `qr_url` deep link, `signature` HMAC over the §7 fields; status `dispatched`; `mandate_issued` event.
- **Accept:** AC-1 inputs; response carries code + QR + signature; a second issue with the same scope is allowed (distinct mandates); non-Dispatcher or cross-payer call → 403 + `scope_denied` event.
- **Deps:** Epic 0. **Size:** M.

### S1.2 `verify_mandate`
- **Deliver:** the gate chain in order: code exists → not expired → not revoked → officer binding (`assigned_officer == caller`) → summary response **without patient PII**; `mandate_verified` event on success; failure → `400 MANDATE_INVALID` (no PII) + `mandate_verify_failed`; **5 consecutive failures lock the code** (`mandate_verify_locked`, further attempts rejected).
- **Accept:** AC-1 matrix (unknown/expired/revoked/other-officer/locked); rate limit tightest here; lockout state survives a page refresh (server-side counter, not client).
- **Deps:** S1.1. **Size:** M.

### S1.3 `revoke_mandate`
- **Deliver:** Dispatcher/Admin endpoint: mandate → `revoked` and **every active session for it → `revoked` immediately**; `mandate_revoked` events; subsequent session calls 403 `MANDATE_REVOKED`.
- **Accept:** AC-6 (immediate kill verified on two concurrent in-flight sessions); idempotent re-revocation → 409.
- **Deps:** S1.1. **Size:** S.

---

## Epic 2 — Session + location (the check-in)

*Goal: a session starts only with a verified mandate **and** an on-site GPS check-in the server itself validates; TTL/idle/revocation all enforced lazily in one place.*

### S2.1 `location.py` — geofence engine
- **Deliver:** haversine (metres); radius resolution `mandate.radius_km` → `HISI Health Facility.surveillance_radius_km` → settings default; status matrix per §9.2 (`verified` / `out_of_range` / `spoofed` / `unreported`) incl. accuracy gate (200 m), ts freshness (2 min), client `is_mock` input; **distance recorded in every outcome**.
- **Accept:** §15.1 unit suite — radius boundary ±1 m, Kenya coordinates, every matrix cell, all three radius sources, stale/mock/low-accuracy payloads.
- **Deps:** S0.2, S0.3. **Size:** M.

### S2.2 `start_session`
- **Deliver:** re-validates the mandate (stale-code defence) + runs the check-in; **strict policy (default, Q3): failure → `400 LOCATION_CHECK_FAILED`, no session**; flag policy: session created + flagged; creates `HISI Surveillance Session` (TTL 240, location fields, device meta, flags), flips mandate `dispatched → active`; `checkin_verified`/`checkin_failed` + `session_started` events.
- **Accept:** AC-2 (both policies, every status); `checkin_distance_m` present on the failed strict attempt too (the review evidence); a replayed/expired code → `MANDATE_INVALID`.
- **Deps:** S2.1, S1.2. **Size:** M.

### S2.3 `require_session` + `get_session` + `end_session` + `recheck_location`
- **Deliver:** the single gate (status active, TTL, idle 30 min, `user == caller`, scope hook) — lazy expiry: next call past TTL/idle marks `expired` + `session_expired` event + 403; `get_session` (state, time remaining, masked scope lines, check-in result); `end_session` (optional final check-in + summary → `completed`); `recheck_location` with the jump-velocity consistency check (> 5 km/h implied move between fixes → flag).
- **Accept:** AC-5 (TTL + idle laziness — no background job involved); recheck impossible-move fixture flags the session; `end_session` twice → 409.
- **Deps:** S2.2. **Size:** M.

### S2.4 Lifecycle back-fill (reporting only)
- **Deliver:** nightly `scheduler_events` job marking stale-active sessions `expired` (enforcement never depends on it).
- **Accept:** job run on a fixture with a lapsed session flips status + logs `session_expired`; a live session untouched.
- **Deps:** S2.3. **Size:** S.

---

## Epic 3 — Scope + surveillance endpoints (the audited surface)

*Goal: inside an active session the officer can surveil exactly the mandate scope — journey, claims, facility, practitioners — and every call, including denials, writes its event.*

### S3.1 `scope.py` — scope resolution
- **Deliver:** resolve mandate scope lines (patient/visit) into in-scope predicates; identifier resolution for patient lines via the `patient_flow` contract (CR ID or National ID); out-of-scope → `403 FORBIDDEN` + `scope_denied` event.
- **Accept:** fixture matrix: each line type × {in-scope, out-of-scope}; patient line with unresolvable identifier → `404` (not 403).
- **Deps:** Epic 1. **Size:** M.

### S3.2 `surveil_patient`
- **Deliver:** thin wrapper over the implemented `patient_flow` (`steps` + `filters` contract unchanged); scope-restricted to mandate patients; **per-step events** (`patient_viewed`, `eligibility_viewed`, `consent_viewed`, `records_viewed`) with step names in `details`; each identifier used is also appended to the session's `surveiled_refs` (recorded in the report, §7.3).
- **Accept:** AC-8 (response shape identical to `patient_flow` for in-scope input); out-of-scope identifier → 403 + denied event; upstream failure → `UPSTREAM_ERROR` propagated + `error` event.
- **Deps:** S2.3, S3.1. **Size:** M.

### S3.3 ~~`list_claims` / `get_claim`~~ — **removed 2026-10-30**
- **Why:** user decision — no claim doctypes; the claim dimension is dispatch context / audit reference only (mandate `reason`), never a data surface (spec Q13). Claim identifiers remain on the mandate for the audit trail.
- **Deps:** none. **Size:** —.

### S3.4 `get_facility` / `list_practitioners`
- **Deliver:** `get_facility` — `HISI Health Facility` detail (license, operational status, level; geofence internals never returned); `list_practitioners` — `HISI Professional Record` rows of the surveilled facility only (the UI never surfaces other practitioners, Q13); viewed practitioner refs appended to the session's `surveiled_refs` (recorded in the report, §7.3).
- **Accept:** `facility_viewed` / `practitioner_viewed` events; a facility with no in-scope visits returns an empty practitioner list (200, not an error).
- **Deps:** S3.1. **Size:** S.

### S3.5 Audit-completeness suite
- **Deliver:** §15.2 as code: for every endpoint × {success, denied, error} exactly one event with correct `event_type`/`outcome`; pre-gate 400/401 → session-null event (`auth_denied`/`error`); failed event insert → `audit_degraded` + `log_error`; append-only assertion (no delete path resolves).
- **Accept:** the whole matrix green in one run; a deliberately omitted event in a new endpoint makes the suite fail (the gate that proves the gate).
- **Deps:** S3.2–S3.4. **Size:** M.

---

## Epic 4 — UI (hisi_app: officer, dispatcher, review)

*Goal: the same flow the spec defines, as three screens — with zero trust in the browser (allow-listed ops, geolocation as a sensor only).*

### S4.1 Operations allow-list + contracts
- **Deliver:** `surveillance.*` operations in `lib/server/operations.ts` (issue_mandate, verify_mandate, start_session, get_session, surveil_patient, get_facility, list_practitioners, recheck_location, end_session, list_sessions, get_session_audit, revoke_mandate) + zod schemas in `lib/operations-contract.ts` — **in the same change as each endpoint lands** (the allow-list drift lesson, 2026-10-23).
- **Accept:** contract test: every operation maps to a live whitelisted method (no 404 drift); browser never calls Frappe directly (existing proxy invariant).
- **Deps:** per-operation with Epics 1–3. **Size:** S (per op).

### S4.2 Officer workspace `/dashboard/surveillance`
- **Deliver:** code entry + QR deep-link landing → verify screen (facility, scope summary, validity) → **start-session** with single-point `navigator.geolocation` capture (accuracy + distance feedback) → in-session surface: scope-line picker → journey (reuses the `/dashboard/patients` step components) → facility panel → practitioners list (surveilled targets only) → end-session with summary.
- **Accept:** geolocation denial → `unreported` path (strict: blocked with the reason shown); out-of-range strict failure shows the `LOCATION_CHECK_FAILED` message with no session; session expiry mid-page → 403 banner, no data.
- **Deps:** S4.1, S3.2. **Size:** L.

### S4.3 Dispatcher console
- **Deliver:** issue-mandate form (facility picker, scope lines, officer picker, validity window, strict/flag policy, optional radius) with the generated **QR displayed for sharing**; mandate list with status; revoke action.
- **Accept:** issued mandate's QR scans into the officer verify flow (manual e2e); cross-payer data never rendered (L2 scoping, client-side belt only).
- **Deps:** S4.1, S1.1/S1.3. **Size:** M.

### S4.4 Sessions + audit review
- **Deliver:** `/dashboard/surveillance/sessions`: list with `location_status` + flags (**flagged rows highlighted** — the "GPS issues dashboard" benchmark); session detail = audit trail table (who/what/when/where/outcome, masked refs) + check-in card (**distance in metres, accuracy, status**) + the recorded identifiers used (`surveiled_refs`) + the surveiled-targets block (facility + practitioners) + JSON export envelope.
- **Accept:** a rejected strict-mode attempt appears as a row with its distance; export envelope matches `get_session_audit` field-for-field.
- **Deps:** S4.1, Epic 3. **Size:** M.

### S4.5 E2E (Playwright, existing setup)
- **Deliver:** dispatcher issues → officer verifies + check-in (mocked browser geolocation) → journey run → end → audit view renders the flagged row for an out-of-range case; direct-URL session without a valid session token → 403 screen.
- **Accept:** spec per §15.6 green in the existing Playwright run against `next dev` (respect the `.next` lock rule — reuse the running dev server).
- **Deps:** S4.2–S4.4. **Size:** M.

---

## Epic 5 — Hardening & full acceptance

*Goal: every §14 acceptance criterion green, PII posture verified, and a signed-off manual field run.*

### S5.1 Rate-limit + lockout verification
- **Deliver:** per-endpoint `frappe.rate_limiter` matrix (tightest `verify_mandate`); lockout counter verified across processes (server-side, cache-backed).
- **Accept:** 429s at documented limits; lockout survives a second browser/tab.
- **Deps:** Epic 1–3. **Size:** S.

### S5.2 Permission matrix + PII pass
- **Deliver:** full role × doctype × endpoint matrix tests (officer zero without session, cross-payer walls, no-delete on Event); PII pass — masked identifiers in every list/audit/export response; unmasked only inside active verified sessions (Q12 default) and under privileged read.
- **Accept:** matrix green; grep-level assertion: no full CR ID / MSISDN in any masked context (test fixtures use synthetic IDs only — the standing no-real-PII rule).
- **Deps:** Epics 0–4. **Size:** M.

### S5.3 Full acceptance sweep
- **Deliver:** complete §15 suite (unit + mocked-upstream integration); `bench --site hisi-local run-tests --app hisi` — the new suite **plus** the existing 47 green.
- **Accept:** **all 10 §14 acceptance criteria** pass in one run; zero regressions in `test_patient.py`.
- **Deps:** all of Epics 0–4. **Size:** M.

### S5.4 Manual field run + go-live checklist
- **Deliver:** real-device run: issue mandate → scan QR on a phone → geofence check-in at an actual facility (inside + deliberately outside for the strict path) → full journey → end → review the audit; go-live checklist: facility coordinate data-quality pass for pilot sites (R4), DPA counsel confirmation (R1/Q9), retention setting verified, `revoke_mandate` drill.
- **Accept:** manual run passes end-to-end; checklist signed off; out-of-range attempt visible in review with its distance.
- **Deps:** S5.3. **Size:** S.

---

## Risks & external dependencies

| # | Item | Owner | Note |
|---|---|---|---|
| R1 | **Kenya DPA 2021 counsel confirmation** — exact statutory basis for the 6-year retention + audit duties (Q9) | User/Counsel | Retention setting ships at 2192 days regardless; counsel may adjust the number, not the architecture. |
| R2 | **Local rebuild starts empty** — `HISI Health Facility` / `HISI Professional Record` accumulate rows as mandates are issued (surveilled targets only); where a pilot facility needs pre-existing detail (coordinates, licence data), a one-time import from the legacy systems is a per-site ops task | Dev/Data | S0.3 defines the schema; the S5.4 data-quality pass before go-live. |
| R3 | **Browser geolocation spoofing gap** — accepted per Q5 (mobile web v1 + flagged review); native wrapper (isMock, selfie, attestation) is the v2 upgrade | User (v2 call) | v1 assurance = accuracy/freshness/consistency + flags + review, not detection. |
| R4 | **Facility coordinate quality** — a missing/stale `HISI Health Facility` lat/lng makes the check-in misfire for that facility | Dev/Data | S5.4 data-quality pass on pilot facilities before go-live; radius default 1.0 km is generous for centroid coordinates. |
| R5 | **Q10 unresolved** — epics assume the draft split: v1 = patient journey + facility + practitioners (claims = dispatch context / audit reference only); payments/preauths/IPS ride on the shelved Patient Journey spec. If the user pulls payments into v1, Epic 3 grows (payments endpoints + scope rules) before Epic 4. | User | Confirm before Epic 3 starts. |

---
*Companion to `claims_surveillance_feature_spec.md`. Story IDs are stable references for the implementation tracker. The Patient Journey spec stays shelved until the user green-lights it; this surveillance feature is the enforcement layer that wraps it when it ships.*
