# Patient Journey — Feature Specification (BMAD)

| | |
|---|---|
| **Feature** | `patient-journey` |
| **App** | `hisi` (module `Hisi` — Health Information System Interoperability) |
| **Status** | DRAFT — pending review |
| **Date** | 2026-10-28 |
| **Requested by** | irad (irad@tiberbu.com) |
| **Input** | 2026-10-28 request: trace a patient journey across Patient, CR\|Eligibility, VisitID, Consent, Claim, IPS, Facility, Visits per period, Claims, Preauths, Practitioners, Top conditions, Claim Value, Payments. Patients sourced from the Client Registry, facilities from the Facility Registry. Settings live in **HISI Settings**; the feature is exposed as APIs. |
| **Reference** | [BMAD Method — ToolSmith](https://docs.bmad-method.org/toolsmith/toolsmith/) |

---

## 1. Summary

Hisi gains a **Patient Journey** service: given a patient identifier and an optional date window, whitelisted REST APIs return the patient's full trace across the Tiberbu platform:

- **Identity & eligibility** from the national **Client Registry** (upstream, via the configured HIE/CR gateway).
- **Facility** details from the **Facility Registry** (national HFR; local replica `Health Facility`).
- **Care & claims trace** from local domain doctypes: visits (Shared Health Record / Appointment), consents, claims, preauthorisations, IPS / insurance-provider involvement, practitioners, top conditions, claim value rollups, and payments.

The service is **read-only and stateless**: it queries existing doctypes and upstream APIs, and writes nothing except an access log. Every upstream connection is configured on the existing **HISI Settings** single doctype (new fields, Section 7) — no credentials in code.

## 2. Background

Hisi currently serves DHA certificate verification, certification checklists / case-criterion assessments, and HMIS product tracking (`HISI Settings` has a single `dha_verify_url` field). The claims domain already exists elsewhere on this bench:

- `global_mediator` — `Claim Record` (claim_id, bundle_id → Shared Health Record, client, facility, insurer, scheme_id, diagnoses, diagnoses_code, claim_amount, claim_status, preauth_token, date_start/date_end), `Claim Response`, `Claim Invoice Record`, `SMART Claim Record`, `Preauth Record`, `Shared Health Record`, `Consent Bypass Log`, `Health Insurance Provider`, `Insurance Scheme Configuration`, `Appointment` (custom visit/claim fields).
- `claims_payment` — `Claim Payments Log`, `SHA Claim Payment`, `Facility Payments`, `SHA Payment Detail`, `Claim Payment Settings`.
- `preauths_mediator` — preauth mediation settings (Careverse).
- `healthpro` — `Health Facility` (HFR replica: facility_code, facility_fid, county, level, status), `Professional Record` (practitioners), `HIE Settings` (the existing Client Registry connection pattern: `hie_base_url`, `fetch_client_uri_cr`, `hie_username`, `hie_password`, `cr_registration_uri`).
- Upstream — Client Registry (verified citizen identity + eligibility/SHIF), Facility Registry (HFR), IPS (insurance-provider system that assigns identifiers to claims, see `global_mediator.HIE.add_ips_identifier_to_claims`).

In this codebase **"client" = patient**: `Claim Record.client` is a Data field holding the client/CR identifier, not a Link. The Patient Journey service is the interoperability façade over all of the above.

## 3. Goals / Non-Goals

**Goals**
1. One primary API that returns the full patient journey for an identifier + date window, with any subset of the 14 journey sections selectable.
2. Thin per-section APIs so integrators can fetch only what they need.
3. All upstream endpoints and credentials configured in `HISI Settings`, overridable per environment.
4. Consistent error contract using the existing `hisi.api.response_handler` machinery (`APIResponse`, `BadRequest`, `NotFound`, `UpstreamError`, …).
5. PII-safe by default (masking on) and a complete audit trail of every journey query.

**Non-Goals**
- Writing to any upstream registry (read-only).
- Patient registration, identity verification, or consent capture (those are Client Registry / HIS concerns).
- Any UI/dashboard — API-first; a UI is a follow-up spec.
- Real-time streaming or webhooks of journey changes.
- Persisting journey snapshots (query-time assembly only; a snapshot doctype is a possible follow-up).

## 4. Glossary (journey dimensions)

| # | Dimension | Meaning in this platform |
|---|---|---|
| 1 | **Patient** | The person, identified by **CR ID** (Client Registry). Name, MSISDN, DOB, gender, SHIF number. |
| 2 | **CR \| Eligibility** | Coverage/eligibility state from the Client Registry (e.g. active SHIF scheme, effective dates). |
| 3 | **VisitID** | A visit/encounter reference — the Shared Health Record bundle for that visit (plus Appointment custom fields where present). |
| 4 | **Consent** | Patient consent state for record access / claim processing, including any consent bypass events. |
| 5 | **Claim** | The current (latest within window) claim: id, status, use, period, payer. |
| 6 | **IPS** | Insurance Provider (system) — the payer of record for the patient's claims; scheme + IPS identifiers on the claims. |
| 7 | **Facility** | The facility(ies) where the patient was seen, resolved via the Facility Registry / `Health Facility` replica. |
| 8 | **Visits per period** | Visit counts grouped by period (month/quarter) inside the window. |
| 9 | **Claims** | Full claim history in the window: id, status, amount, insurer, dates, response. |
| 10 | **Preauths** | Preauthorisations in the window: id, status, amount, decision dates. |
| 11 | **Practitioners** | Distinct practitioners associated with the patient's claims/visits in the window (from the Health Worker Registry replica). |
| 12 | **Top conditions** | Diagnoses aggregated across the patient's claims in the window, ranked by frequency (default top 10). |
| 13 | **Claim Value** | Monetary rollup: billed, approved, paid, outstanding (KES). |
| 14 | **Payments** | Payment records for the patient's claims in the window: id, amount, date, status, channel. |

## 5. User stories

1. **As a claims auditor**, I can retrieve a patient's complete journey for a date window so I can verify the visit → consent → claim → preauth → payment chain end to end.
2. **As a fraud investigator**, I can see visits-per-period and claim-value trends so I can spot anomalies (e.g. high claim count against few visits).
3. **As an integrator**, I can call section-specific APIs to fetch only the data my system needs, without pulling the full journey payload.
4. **As a System Manager**, I can configure the Client Registry, Facility Registry, and IPS connections in HISI Settings without a code deploy.

## 6. Data sources & lineage (per dimension)

| # | Dimension | Source | Doctype / endpoint | Key fields / identifiers |
|---|---|---|---|---|
| 1 | Patient | **Client Registry** (upstream, via HIE gateway) | CR fetch-client endpoint (HISI Settings: `cr_base_url` + `cr_fetch_client_uri`) | CR ID, full name, MSISDN, DOB, gender, SHIF number |
| 2 | CR \| Eligibility | **Client Registry** (upstream) | CR eligibility endpoint (`cr_eligibility_uri`) | scheme name, status, effective from/to, SHIF number |
| 3 | VisitID | Local | `Shared Health Record` (bundle), `Appointment` (custom visit fields) | SHR name, bundle id, visit date, facility |
| 4 | Consent | Local | `Consent Bypass Log` (+ consent fields present on claims/SHR records) | consent status, bypass reason, timestamp |
| 5 | Claim (current) | Local | `Claim Record` (latest within window) | claim_id, claim_status, use, date_start, insurer |
| 6 | IPS | Local + upstream | `Health Insurance Provider`, `Insurance Scheme Configuration`; IPS identifiers attached to claims (see `global_mediator.HIE`) | provider, scheme_id, IPS identifier |
| 7 | Facility | **Facility Registry** (HFR) — local replica first, upstream on miss | `Health Facility`; FR facility lookup (`fr_base_url` + `fr_facility_uri`) | facility_code / facility_fid, name, county, level, operational status |
| 8 | Visits per period | Local | `Shared Health Record` / `Appointment` grouped by period | period label, visit count |
| 9 | Claims | Local | `Claim Record` + `Claim Response`, `Claim Invoice Record`, `SMART Claim Record` | claim_id, status, amount, insurer, dates, response summary |
| 10 | Preauths | Local | `Preauth Record` (+ `Claim Record.preauth_token` cross-link; `preauths_mediator` for Careverse) | preauth id, status, amount, dates, linked claim |
| 11 | Practitioners | Local + upstream | `Professional Record` (via practitioner ids on claims/SHR); upstream practitioner fetch configured in HISI Settings (`pr_base_url` + `pr_fetch_practitioner_uri`) | name, cadre, regulator, registration number |
| 12 | Top conditions | Local | `Claim Record.diagnoses` / `diagnoses_code` | ICD-10 code, description, count, first/last seen |
| 13 | Claim Value | Local | `Claim Record.claim_amount` + `Claim Response` adjudications + payment doctypes | billed, approved, paid, outstanding (KES) |
| 14 | Payments | Local | `Claim Payments Log`, `SHA Claim Payment`, `Facility Payments`, `SHA Payment Detail` | payment id, amount, date, status, channel |

> **Identifier join keys.** The join spine is **CR ID** (patient) + **facility fid** + **claim_id** + **SHR bundle id**. `Claim Record.client` and `.facility` are Data fields (identifiers, not Links), so section builders must match on them with exact-string queries.

## 7. HISI Settings — new settings (PRD requirement)

The existing single doctype `HISI Settings` (`hisi/hisi/doctype/hisi_settings/hisi_settings.json`) gains a **Patient Journey** tab with the fields below. Field naming follows the existing `HIE Settings` / `Claim Payment Settings` conventions (`*_base_url`, `*_uri`, `*_username`, `*_password`).

| fieldname | fieldtype | label | default / note |
|---|---|---|---|
| `patient_journey_tab` | Tab Break | Patient Journey | |
| `cr_section` | Section Break | Client Registry | |
| `cr_base_url` | Data | Client Registry Base URL | HIE/CR gateway origin |
| `cr_fetch_client_uri` | Data | CR Fetch Client URI | path for identity lookup |
| `cr_eligibility_uri` | Data | CR Eligibility URI | path for eligibility lookup |
| `cr_username` | Data | CR Username | |
| `cr_password` | Password | CR Password | |
| `fr_section` | Section Break | Facility Registry | |
| `fr_base_url` | Data | Facility Registry Base URL | |
| `fr_facility_uri` | Data | Facility Lookup URI | |
| `fr_username` | Data | FR Username | |
| `fr_password` | Password | FR Password | |
| `ips_section` | Section Break | IPS / Insurance Provider | |
| `ips_base_url` | Data | IPS Base URL | |
| `ips_username` | Data | IPS Username | |
| `ips_password` | Password | IPS Password | |
| `pr_section` | Section Break | Practitioner Registry | |
| `pr_base_url` | Data | Practitioner Registry Base URL | |
| `pr_fetch_practitioner_uri` | Data | Practitioner Fetch URI | path for practitioner identity lookup |
| `pr_username` | Data | Practitioner Registry Username | |
| `pr_password` | Password | Practitioner Registry Password | |
| `journey_section` | Section Break | Journey Behaviour | |
| `enable_upstream` | Check | Enable Upstream Lookups | default 1; 0 = local-data-only journey |
| `mask_pii` | Check | Mask PII in Responses | default 1 |
| `default_window_days` | Int | Default Window (days) | default 365 |
| `max_window_days` | Int | Max Window (days) | default 730; wider requests → 400 |
| `journey_cache_ttl` | Int | Upstream Cache TTL (seconds) | default 300 |
| `top_conditions_limit` | Int | Top Conditions Limit | default 10 |

Behaviour notes:
- All four upstream blocks (Client Registry, Facility Registry, IPS, Practitioner Registry) are optional at runtime: if a block is unconfigured (or `enable_upstream` = 0), the affected sections are served from local data only, and the response marks them `source: "local"` (Section 8.3).
- **Mandatory configuration rule:** every HISI API that fetches a client, an eligibility result, a practitioner, or a facility must be configured in HISI Settings (base URL + URI path + credentials per block). No fetch-endpoint URL may live in code.
- Password fields use Frappe's standard `Password` type (stored hashed, never returned by read APIs).

## 8. API design

Base path: `/api/method/hisi/api/patient_journey/…` — new package `hisi/api/patient_journey/`. All endpoints are `@frappe.whitelist()` (authenticated users only — **no** `allow_guest`), wrapped in the existing `api_endpoint` decorator, rate-limited with `@frappe.rate_limiter.rate_limit`, and return via `APIResponse` (200 success envelope: `{"status": "success", "data": …, "meta": …}`). This is an **app-wide house rule**: every HISI API — existing and future — handles all of its responses through `hisi/api/response_handler.py` (Section 17).

### 8.1 Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `trace_patient_journey` | POST | **Primary.** Full journey, any subset of sections. |
| `lookup_patient` | GET | Resolve identifier → canonical patient (CR identity). |
| `check_eligibility` | GET | CR \| Eligibility only. |
| `list_visits` | GET | Visits (VisitID) + visits-per-period grouping. |
| `list_claims` | GET | Claims history + current claim + claim-value rollup. |
| `list_preauths` | GET | Preauths in window. |
| `list_payments` | GET | Payments in window. |
| `list_conditions` | GET | Top conditions in window. |
| `list_practitioners` | GET | Distinct practitioners in window. |
| `list_facilities` | GET | Facilities used in window (FR-resolved). |

### 8.2 Request contracts

Common parameters (all endpoints):

```json
{
  "identifier_type": "cr_id",          // "cr_id" | "msisdn" | "name_dob"
  "identifier": "CR2026000123",        // for name_dob: {"name": "...", "dob": "YYYY-MM-DD"}
  "start_date": "2026-01-01",          // optional; default = today - default_window_days
  "end_date": "2026-10-28",            // optional; default = today
  "page": 1,
  "page_size": 50                       // optional, max 200
}
```

`trace_patient_journey` additionally accepts:

```json
{
  "sections": ["patient", "eligibility", "visits", "visits_per_period", "consent",
               "claim", "ips", "facility", "claims", "preauths", "practitioners",
               "conditions", "claim_value", "payments"]
}
```

Defaults to all sections. Unknown section names → 400 `BAD_REQUEST`.

### 8.3 Response contract (primary endpoint)

```json
{
  "status": "success",
  "data": {
    "patient":  { "cr_id": "CR2026000123", "name": "A. Otieno", "msisdn": "+2547** ***123",
                  "dob": "1990-04-12", "gender": "Female", "shif_number": "…" ,
                  "source": "client_registry", "fetched_at": "2026-10-28T10:00:00Z" },
    "eligibility": { "schemes": [ { "name": "SHIF", "status": "Active",
                    "effective_from": "2026-01-01", "effective_to": "2026-12-31" } ],
                    "source": "client_registry" },
    "visits": [ { "visit_id": "SHR-8812", "date": "2026-03-04", "facility": "FID-0042",
                  "type": "OP" } ],
    "visits_per_period": [ { "period": "2026-03", "count": 4 },
                            { "period": "2026-06", "count": 2 } ],
    "consent": [ { "status": "granted", "at": "2026-03-04T08:12:00Z", "scope": "claim_processing" },
                 { "bypassed": true, "reason": "emergency", "at": "…" } ],
    "claim": { "claim_id": "CLM-2026-0091", "status": "Submitted", "use": "SHIF",
               "date_start": "2026-03-04", "insurer": "SHA", "ips_identifier": "IPS-…" },
    "ips": { "provider": "SHA", "scheme_id": "SHIF-…", "ips_identified": true },
    "facility": { "used": [ { "facility_fid": "FID-0042", "name": "Hope Clinic",
                               "county": "Nairobi", "level": "Level 3",
                               "operational_status": "Operational", "visits": 5,
                               "source": "facility_registry" } ] },
    "claims": [ { "claim_id": "CLM-2026-0091", "status": "Submitted", "amount": 48200.00,
                  "insurer": "SHA", "date_start": "2026-03-04", "date_end": "2026-03-09",
                  "preauth_id": "PA-2026-0031" } ],
    "preauths": [ { "preauth_id": "PA-2026-0031", "status": "Approved", "amount": 48200.00,
                    "decided_at": "2026-03-03", "linked_claim": "CLM-2026-0091" } ],
    "practitioners": [ { "name": "Dr. A. Wanjiru", "cadre": "Medical Officer",
                          "regulator": "KMPDC", "registration_number": "KMPDC/…" } ],
    "conditions": [ { "code": "E10.9", "name": "Diabetes mellitus", "count": 3,
                       "first_seen": "2026-01-10", "last_seen": "2026-06-20" } ],
    "claim_value": { "billed": 142600.00, "approved": 128000.00, "paid": 96000.00,
                     "outstanding": 32000.00, "currency": "KES" },
    "payments": [ { "payment_id": "PAY-2026-0112", "amount": 96000.00,
                     "date": "2026-07-02", "status": "Settled", "channel": "Finsprint" } ]
  },
  "meta": {
    "window": { "start": "2026-01-01", "end": "2026-10-28" },
    "sections_requested": [ "all" ],
    "upstream_sources": [ "client_registry", "facility_registry" ],
    "upstream_errors": [],
    "cache_hit": false
  }
}
```

Rules:
- Sections with no data return `[]` / `null` — **never** an error. An empty journey is a valid 200.
- Each upstream-sourced section carries `source` and `fetched_at` provenance.
- If an upstream call fails, the affected section is returned with `"degraded": true` + `reason`, and `meta.upstream_errors` is populated. The overall status stays 200 unless **every** requested section failed.
- Money values are floats in KES unless a currency is present upstream (then `"currency"` reflects it).

### 8.4 Error contract (reuses `hisi/api/response_handler.py`)

| HTTP | code | When |
|---|---|---|
| 400 | `BAD_REQUEST` | Unknown section, invalid identifier, window > `max_window_days`, malformed dates |
| 401 | `UNAUTHORIZED` | Missing/invalid session |
| 403 | `FORBIDDEN` | User lacks journey-read permission (see 8.5) |
| 404 | `NOT_FOUND` | No patient resolvable for the identifier (CR miss + local miss) |
| 429 | rate-limit | `frappe.rate_limiter` exhausted (default 60/hour/user) |
| 400/502 | `UPSTREAM_ERROR` | All upstream sources failed (keeps existing `UpstreamError` code; 502 when fully attributable to upstream) |

### 8.5 AuthN / AuthZ

- Session- or API-token authenticated (standard Frappe `/api/method` auth). No guest access.
- Role gate: define role **`Hisi Journey Reader`**; endpoints check `frappe.has_role()`. System Manager always allowed.
- `mask_pii = 1` (default): MSISDN masked to last 3 digits, IDs to last 4. Unmasked values only when `mask_pii = 0` **and** the caller holds System Manager.
- Every request (success or failure) writes a `Hisi Journey Access Log` row: user, masked identifier, identifier_type, sections, sources used, duration, outcome.

## 9. Data model & query plan

- **No new data doctypes required** for the journey itself — all sections are assembled at query time from Section 6 sources.
- **One new audit doctype**: `Hisi Journey Access Log` (fields: user, identifier_masked, identifier_type, sections, sources, duration_ms, status, error_code) — insert-only, System Manager only.
- Section builders (in `sections.py`) use `frappe.get_all` / `frappe.db.get_all` with exact-match filters on the join keys (Section 6 note), window filters on `date_start`/`creation`, and `page_size` capped at 200.
- "Top conditions" = `GROUP BY diagnoses_code` over the patient's in-window claims, `ORDER BY count DESC`, limit `top_conditions_limit`.
- "Visits per period" = `DATE_FORMAT(..., '%Y-%m')` grouping (quarter supported via a `period_granularity` param: `month` | `quarter`, default `month`).
- "Claim value" rollup: `SUM(claim_amount)` (billed); approved/paid from `Claim Response` adjudication + payment doctypes; `outstanding = billed - paid` (floored at 0).
- **Upstream clients** (`upstream.py`): `ClientRegistryClient`, `EligibilityClient`, `PractitionerRegistryClient`, `FacilityRegistryClient`, `IPSClient` — thin `requests`-based wrappers built from HISI Settings values (pattern: `global_mediator.HIE` / `HIE Settings` usage). Responses cached in Frappe cache keyed by (endpoint, identifier, params) with `journey_cache_ttl`.
- **Facility resolution**: check `Health Facility` replica first (fast, local); on miss and `enable_upstream`, call the Facility Registry endpoint and return the upstream record without persisting it (no sync side-effects).

## 10. Caching & performance

- Upstream responses: Frappe cache, TTL = `journey_cache_ttl` (default 300 s). Eligibility is the hottest — cache separately keyed by CR ID.
- Local section queries target indexed fields (`search_index` already set on `Claim Record.client`, `.facility`, `.claim_status`).
- Target p95 < 1.5 s for a full 14-section journey at 500-claim window volume; each section endpoint < 500 ms local-only.
- No background jobs in v1 (no snapshotting).

## 11. Security & PII

1. Read-only by construction; no endpoint mutates domain data (only the access log insert).
2. PII masking default-on (8.5). Response bodies never include `cr_password` / `fr_password` / `ips_password` values (Password fieldtype guarantees this on reads).
3. Rate limiting per user; upstream calls inherit the same guard (a rate-limited user cannot hammer CR/FR through us).
4. Audit: every journey query is logged (8.5); logs are insert-only.
5. No journey data in URLs (identifier travels in body for POST; GET uses query params and is marked PII-sensitive in the access log, masked there).

## 12. Acceptance criteria

1. HISI Settings shows the Patient Journey tab with all Section-7 fields; values persist across bench restarts.
2. `trace_patient_journey` with a valid CR ID returns all 14 sections for the window; each of the 9 per-section endpoints returns the matching slice of the primary response.
3. Empty data in any section → `[]`/`null`, status 200.
4. Upstream outage (CR down) → `patient`/`eligibility` sections degraded with reason; local sections unaffected; `meta.upstream_errors` populated.
5. Unknown identifier → 404 `NOT_FOUND`. Window exceeding `max_window_days` → 400.
6. With `mask_pii = 1`, no full MSISDN/ID appears anywhere in the response; with `mask_pii = 0` + System Manager, full values appear.
7. Every request produces exactly one `Hisi Journey Access Log` row with the masked identifier.
8. Unauthenticated call → 401; authenticated non-`Hisi Journey Reader` (non-SM) → 403.
9. Unit tests for every section builder (fixtures for claims/SHR/preauths/payments) and for the upstream clients (mocked HTTP) pass: `bench --site qwen-local run-tests --app hisi` (site where hisi is installed).

## 13. Test plan (outline)

- **Unit** (`hisi/tests/test_patient_journey.py`): section builders against seeded fixtures (Claim Record, SHR, Preauth Record, payment doctypes); masking logic; window clamping; period grouping; conditions ranking; claim-value rollup math.
- **Integration (mocked upstream)**: `ClientRegistryClient` happy/404/timeout via `responses`/`httpx` mocks; degraded-section behaviour in the primary endpoint.
- **Manual** (dev bench): configure HISI Settings against the real CR/FR endpoints on the `qwen-local` site; run the primary endpoint with a known patient; verify provenance fields and access-log rows.

## 14. Open questions

1. **Patient lookup keys** — is CR ID + MSISDN sufficient, or must name+DOB fuzzy matching be supported (data quality varies)?
2. **VisitID definition** — is the authoritative visit identifier the SHR bundle name, or a dedicated field on `Appointment`? (Verify the Appointment custom-field list before building Section 3.)
3. **Consent source of truth** — `Consent Bypass Log` only captures bypasses. Where is the primary consent record (SHR resource type? claim field)? If none exists, the section reports bypass events only.
4. **Preauth authority** — `Preauth Record` (global_mediator) vs `preauths_mediator` (Careverse): which is authoritative for a given payer, or do both surface per-payer?
5. **Gateway vs direct** — do CR/FR calls go through the same HIE gateway (`hie_base_url` style) as healthpro's, or direct origins? Affects whether `cr_base_url` duplicates `HIE Settings`.
6. **IPS scope** — is IPS needed as a live lookup (resolve provider/scheme for the patient), or only as claim-level identifier reporting from local data?
7. **Currency** — KES only, or multi-currency exposure for foreign schemes?
8. **Guest/API-key access** — is any non-session access (machine-to-machine API keys) required, or is Frappe session/token auth sufficient for v1?

## 15. Out of scope (v1)

- UI/dashboard for the journey (follow-up spec once APIs are proven).
- Patient journey **snapshots** (persisted point-in-time journey doctype).
- Webhook/streaming notifications of journey changes.
- Cross-bench aggregation (data outside this bench's databases).
- Any write path to Client Registry, Facility Registry, or IPS.

## 16. Implementation notes (file map)

| File | Change |
|---|---|
| `hisi/hisi/doctype/hisi_settings/hisi_settings.json` (+ `.js` if JS is present) | Add Patient Journey tab & Section-7 fields; bump `modified` |
| `hisi/api/patient_journey/__init__.py` | New package (exports public endpoint functions) |
| `hisi/api/patient_journey/api.py` | Whitelisted endpoints (8.1) with `@frappe.whitelist()`, `@frappe.rate_limiter`, `api_endpoint` |
| `hisi/api/patient_journey/models.py` | Pydantic response models per section |
| `hisi/api/patient_journey/sections.py` | Local-data section builders (6) |
| `hisi/api/patient_journey/upstream.py` | `ClientRegistryClient`, `EligibilityClient`, `PractitionerRegistryClient`, `FacilityRegistryClient`, `IPSClient` + caching — all endpoint URLs and credentials from HISI Settings (Section 7), none in code |
| `hisi/hisi/doctype/hisi_journey_access_log/` | New audit doctype (+ `Hisi Journey Access Log` permission: read = System Manager) |
| `hisi/permissions.py` / roles | Add `Hisi Journey Reader` role |
| `hisi/tests/test_patient_journey.py` | Unit + mocked-upstream tests (13) |

Style: follow existing hisi conventions (4-space indent, `APIResponse`-based error raising, no new HTTP layer).

---
## 17. Cross-cutting engineering rules (all HISI APIs)

These rules apply to **every** API in the `hisi` app — patient journey included, and every future endpoint:

1. **Single response layer.** All responses go through `hisi/api/response_handler.py`: success via `APIResponse.success(...)` / `APIResponse.respond_with(...)`, errors via the `APIResponse` subclasses (`BadRequest`, `Unauthorized`, `Forbidden`, `NotFound`, `Conflict`, `UpstreamError`). Every endpoint function is wrapped in the `api_endpoint` decorator. No endpoint writes `frappe.local.response` directly or uses any other envelope.
2. **No docstrings** — in functions, classes, or files.
3. **No private methods or functions** — no leading-underscore names; everything is public.
4. **Spacing** — exactly one blank line between functions and between classes.
5. **Structured types** — pydantic models for validated/serialized request and response shapes, dataclasses for plain structured state, `@property` and `@staticmethod` where they express the design.

---
*Generated as a BMAD feature spec for the `hisi` app. Next steps: review Sections 7 & 8 (settings + API contract), resolve the open questions in Section 14, then split into epics/stories (BMAD plan phase) for implementation.*
