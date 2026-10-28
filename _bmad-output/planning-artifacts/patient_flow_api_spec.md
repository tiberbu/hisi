# Patient Flow API — Feature Specification (BMAD)

| | |
|---|---|
| **Feature** | `patient-flow` (patient → CR ID → eligibility → consent) |
| **App** | `hisi` (module `Hisi`) |
| **Status** | v1.1 — implemented & live-verified 2026-10-28 (`hisi-local`); eligibility body + response contract and `{"id"}` patient search pinned |
| **Date** | 2026-10-28 |
| **Requested by** | irad (irad@tiberbu.com) |
| **Implementation** | `hisi/api/patient.py` → `patient_flow` (whitelisted `hisi/api/patient.patient_flow`), tests `hisi/tests/test_patient.py` |
| **Related** | Patient Journey spec (14-dimension, DRAFT, shelved) — this flow is the first vertical slice of it |

---

## 1. Summary

One whitelisted API — `patient_flow` — executes a **frontend-controlled sequence flow** over DHA upstream services. The caller passes which steps to run, in what order, plus the filters each step needs:

- `patient` — fetch the client from the Client Registry (production base) → resolves the **CR ID**.
- `eligibility` — validate coverage for the CR ID (production base, POST).
- `consent` — fetch `Visit Consent` records (UAT base when `enable_uat_simulation` is on; the only step that runs against the UAT origin).

Every tunable (base URLs, URIs, timeout, JWT TTL) is read from **HISI Settings** — **no tunable constants in code** (house rule set 2026-10-28). All responses go through `hisi/api/response_handler.py` (`api_endpoint` + `APIResponse`).

## 2. Endpoint

| | |
|---|---|
| Route | `/api/method/hisi/api/patient.patient_flow` |
| Methods | `POST` (JSON body) and `GET` (query string) |
| Auth | Standard Frappe session/token — no guest access |
| Body param | Single `data` param: a JSON **object** (a JSON string is also accepted — form-encoded REST flattens it; both parse identically) |
| Errors | `400 BAD_REQUEST`, `404 NOT_FOUND`, `400 UPSTREAM_ERROR` (message carries the HTTP code), `500 INTERNAL_ERROR` |

## 3. Request payload

```json
{
  "data": {
    "steps": ["patient", "eligibility", "consent"],
    "filters": { "identification_type": "National ID", "identification_number": "45887531" }
  }
}
```

`data` is passed as a real JSON object. (For form-encoded REST callers the same value may arrive as a JSON **string** — `"{\"steps\": ...}"` — the endpoint parses both identically.)

### 3.1 `steps` (presence + sequence)

- Any non-empty subset/order of `patient`, `eligibility`, `consent`; the **list order is the execution order**.
- Duplicates are deduped (first occurrence wins); unknown step name → `400 BAD_REQUEST`.
- Dependency rule: if `patient` is absent, `cr_id` (or `client`) **must** be present in `filters` — the CR ID cannot be resolved otherwise.

### 3.2 `filters` (per step)

| Step | Required filters | Optional filters |
|---|---|---|
| `patient` | `identification_type` + `identification_number` (both non-empty) → payload `{"identification_type","identification_number"}`; or `cr_id`/`client` → payload `{"id": <cr>}` (live-verified — DHA does NOT index CR numbers under any `identification_type` value) | — |
| `eligibility` | `cr_id`/`client` when `patient` step absent | body is fixed `{"requestIdType": <int>, "requestIdNumber": "<str>"}` — explicit `requestIdType`/`requestIdNumber` filters win; else the searched identifier (type+number) mapped via `REQUEST_ID_TYPES` (CR ID → 3); else CR ID (3) + `cr_id`; unmapped type → `400` |
| `consent` | `cr_id`/`client` when `patient` step absent | — (client filter is always `[["client", "=", <cr_id>]]`) |

### 3.3 Examples (live-verified 2026-10-28)

Full journey, searched by National ID (resolves IRAD MWENDO):

```json
{"steps": ["patient", "eligibility", "consent"], "filters": {"identification_type": "National ID", "identification_number": "36637305"}}
```

Patient only, searched by CR — sent as `{"id": "..."}` payload (DHA does not index CR numbers under any `identification_type`):

```json
{"steps": ["patient"], "filters": {"cr_id": "CR6839901180428-3"}}
```

Eligibility only (CR known; POST body → `{"requestIdType": 3, "requestIdNumber": "CR6839901180428-3"}`):

```json
{"steps": ["eligibility"], "filters": {"cr_id": "CR6839901180428-3"}}
```

Consent only (CR known; client filter `[["client", "=", <cr_id>]]`):

```json
{"steps": ["consent"], "filters": {"cr_id": "CR2473157541615-8"}}
```

## 4. Response payload

### 4.1 Success (200)

```json
{
  "status": "success",
  "data": {
    "results": {
      "patient": {
        "cr_id": "CR45887531-9",
        "name": "JOAN WAMBUI",
        "gender": "FEMALE",
        "msisdn": "0724512355",
        "date_of_birth": "1993-11-06",
        "client_status": "Active",
        "identification_type": "National ID",
        "identification_number": "45887531"
      },
      "eligibility": {
        "eligible": true,
        "status": "Member found. Check Schemes for coverages",
        "scheme": "UHC",
        "schemes": [
          { "scheme": "UHC", "scheme_id": 1, "member_type": "PRIMARY", "covered": true, "status": "1", "message": "The individual is covered.", "reason": "Payment is up to Date. " },
          { "scheme": "SHIF", "scheme_id": 2, "member_type": "PRIMARY", "covered": true, "status": "1", "message": "The individual is covered.", "reason": "Payment is up to Date. " }
        ]
      },
      "consent": {
        "records": [
          {
            "name": "VCR-20261028-8DCBBCFF",
            "client": "CR2473157541615-8",
            "principal_cr_id": "CR2473157541615-8",
            "status": "Active",
            "consent_status": "Approved",
            "channel": "OTP",
            "verification_status": "Verified",
            "verified_by": "CR2473157541615-8",
            "verification_timestamp": "2026-10-28 18:25:27",
            "facility_id": "FID-47-105963-0",
            "requesting_system": "HALCYON HEALTHCARE LIMITED",
            "visit_id": "b92d1e8f-accc-48d4-84dd-2eb3e0d9fdca",
            "visit_request_id": "74784F3318C5"
          }
        ],
        "base_url": "https://uat.dha.go.ke"
      }
    },
    "steps": ["patient", "eligibility", "consent"],
    "cr_id": "CR45887531-9",
    "simulation": true,
    "base_url": "https://api.dha.go.ke",
    "fetched_at": "2026-10-28T19:35:06.400737"
  }
}
```

Rules:

- `results` contains **only** the requested steps, in request order (a step that is not requested is absent, not null).
- Empty data in any section → empty `records` / empty strings — still 200 (an empty result is a valid success).
- `patient.name` = `first_name middle_name last_name` (fallback `full_name`/`name`); `cr_id` = the fetched client's `id` (key order `CR_ID_KEYS` starts with `id`; same value at top level and in `results.patient.cr_id`).
- `eligibility` parses DHA's `statusCode`/`statusDesc` + `schemes[]`: `eligible` = at least one scheme with `coverage.status` `"1"` (or a "covered" message); `scheme` = first covered scheme name; `schemes[]` carries the per-scheme coverage detail.
- `consent.base_url` = the UAT origin actually used; `simulation` = value of `enable_uat_simulation`.
- An upstream failure on **any** requested step aborts the flow with the error envelope (no partial results in v1).

### 4.2 Error envelope (via `response_handler`)

```json
{ "status": "error", "code": "BAD_REQUEST", "message": "HISI Settings uat_base_url is not configured.", "errors": [] }
```

| HTTP | code | When |
|---|---|---|
| 400 | `BAD_REQUEST` | missing/empty `data`, unknown step, empty `steps`, malformed JSON, missing patient filters, `cr_id` filter missing without `patient` step, unset `uat_base_url` / `fetch_patient_consent` / `base_url` / `fetch_client` / `eligibility_check` / consumer credentials |
| 404 | `NOT_FOUND` | fetch-client returned no records; patient found but no CR ID resolvable |
| 400 | `UPSTREAM_ERROR` | upstream HTTP error (message includes the upstream status, e.g. `... HTTP 502`), timeout, or non-JSON body |
| 500 | `INTERNAL_ERROR` | unexpected exception (logged via `frappe.log_error`) |

## 5. Configuration (HISI Settings — single source of truth)

| fieldname | type | used by | behaviour |
|---|---|---|---|
| `base_url` | Data | patient, eligibility | production DHA origin (`https://api.dha.go.ke`) |
| `fetch_client` | Data | patient | URI path (`/client-registry/fetch-client`) |
| `eligibility_check` | Data | eligibility | URI path (`/backend/api/v5/coverage/validate`, POST) |
| `consumer_key` / `consumer_secret` | Data / Password | all JWT-authed calls | DHA API key pair (secret read via `get_password`) |
| `upstream_api_timeout` | Int | all HTTP | request timeout seconds; **default 60** when unset (site validation: min 60) |
| `jwt_ttl_s` | Int | JWT `exp` | token lifetime seconds; **default 60** when unset (site validation: min 60) |
| `uat_base_url` | Data | consent | UAT origin (`https://uat.dha.go.ke`); **required** for consent, no fallback |
| `fetch_patient_consent` | Data | consent | full CMP resource path (`/v1/cmp/resource/Visit Consent`); **required**, used verbatim |
| `enable_uat_simulation` | Check | response meta | reported as `simulation`; consent always runs against `uat_base_url` |

House rule: **no tunable constant may be declared in code** — every endpoint/URL/TTL lives on HISI Settings; unset required values fail fast with a named `BAD_REQUEST`.

## 6. Upstream DHA contracts (live-verified 2026-10-28)

- **Auth** — self-signed **HS256 JWT** (not OAuth): header `{"alg":"HS256","typ":"JWT"}`, payload `{"key": <consumer_key>, "iat": now, "exp": now + jwt_ttl_s}`, signature `HMAC-SHA256(base64url(header)+"."+base64url(payload), consumer_secret)` → `Authorization: Bearer <jwt>`. (Reference: user's Postman pre-request script.)
- **Patient** — `GET {base_url}{fetch_client}?payload=<json>` (JSON string param). Payload shapes (live-verified 2026-10-28): `{"identification_type","identification_number"}` (e.g. National ID) or **`{"id": "<record id>"}`** — the record's `id` IS the CR string (e.g. `CR6839901180428-3`); CR numbers return 0 records under `identification_type` "CR ID" / "CR Number" / "CR". Response `{"status":200,"message":"Client successfully fetched","count":N,"data":[...]}` (the `{"message":{"total","result":[]}}` variant is parsed as well); `cr_id` may be null — `id` is the authoritative client reference.
- **Eligibility** — `POST {base_url}{eligibility_check}` (GET 404s; route confirmed). **Body schema pinned 2026-10-28** from the user's working Postman call: `{"requestIdType": 3, "requestIdNumber": "<id>"}` — `requestIdType` 3 = CR ID (confirmed live); the full `requestIdType` table (National ID, ...) is still pending from the user's Postman/DHA docs. All other probed shapes (identification_type/identification_number, cr_id/client, payload/data wraps) returned `400 "Invalid request content"`. **Response shape confirmed live 2026-10-28** (CR `CR6839901180428-3`): `{requestIdType, requestIdNumber, memberCrNumber, fullName, statusCode, statusDesc, schemes[{schemeName, schemeId, memberType, policy{startDate, endDate, number}, coverage{startDate, endDate, message, reason, status}, principalContributor{idNumber, idType, crNumber, name, employmentType, employerDetails{...}}}]}`; `coverage.status "1"` = covered (e.g. `statusCode "10"` = "Member found. Check Schemes for coverages"). The code maps the searched identifier via `REQUEST_ID_TYPES`, accepts explicit `requestIdType`/`requestIdNumber` filters as an override, and fails fast (400) on unmapped types.
- **Consent** — `GET {uat_base_url}{fetch_patient_consent}?filters=<json>&fields=<json>`; `filters = [["client","=",<cr_id>]]`, `fields` = the 13 fields in `CONSENT_FIELDS` (the request contract itself, not a tunable). Unauthenticated (UAT CMP accepts without a token — live-verified 200 with real records).

## 7. Security & PII

1. Session/token auth only; no `allow_guest`.
2. Consumer secret is never echoed (read via `get_password`, travels only in the JWT signature).
3. Upstream keys: DHA scopes Client Registry visibility **per consumer key** — the site key `1IB-DHABP02215` returned `total:0` for an identifier that resolved in the user's Postman; key parity between HISI Settings and the Postman environment must be confirmed (open item).
4. `msisdn`/`date_of_birth` are returned as provided by the CR; masking policy is out of scope for this slice (covered by the Patient Journey spec when it resumes).

## 8. Test & verification status

- `hisi/tests/test_patient.py` — 27 tests: full sequence flow (URLs, params, Bearer presence/absence, consent params), byte-level JWT contract + signature verification, JWT TTL from settings, per-step standalone flows, dedupe, validation errors, not-found, ineligible-still-success, UAT base override, upstream 4xx/5xx/non-JSON, missing credentials/settings.
- Suite: **37/37 green** on `hisi-local` (`bench --site hisi-local run-tests --app hisi`); ruff clean.
- Live: JWT accepted by production DHA (401 → 2xx); consent step returned a real record (`CR2473157541615-8`, Approved/OTP/Verified); JWT TTL default 60 confirmed.

### 8.1 Live-verified inputs (2026-10-28, `hisi-local`, real DHA data)

| `data` | Verified result |
|---|---|
| `{"steps": ["patient"], "filters": {"identification_type": "National ID", "identification_number": "36637305"}}` | IRAD MWENDO — record `id` = `CR6839901180428-3` (record `cr_id` null) |
| `{"steps": ["patient"], "filters": {"cr_id": "CR6839901180428-3"}}` | same record via the `{"id": ...}` payload (CR lookup is id-only) |
| `{"steps": ["eligibility"], "filters": {"cr_id": "CR6839901180428-3"}}` | `statusCode "10"` — "Member found. Check Schemes for coverages"; UHC + SHIF covered → `eligible: true`, `scheme: "UHC"` |
| `{"steps": ["consent"], "filters": {"cr_id": "CR2473157541615-8"}}` | 1 record — Approved / OTP / Verified (`VCR-20261028-8DCBBCFF`) |

All requests pass `data` as a real JSON object; the double-encoded string form is accepted identically for form-encoded REST callers.

## 9. Open items

1. **Eligibility `requestIdType` mapping** — body schema pinned (`{"requestIdType", "requestIdNumber"}`, CR ID = 3, 2026-10-28, from the user's working Postman call); the full `requestIdType` table (National ID, MSISDN, ...) is still needed from the user's Postman / DHA docs to map searched identifiers automatically.
2. **Consumer key parity** — confirm the HISI Settings `consumer_key`/`consumer_secret` match the Postman environment variables (CR data is scoped per key).
3. **Settings sync** — repo `hisi_settings.json` lags the site doctype (new fields added on-site 2026-10-28); reconcile before the next `bench migrate`.

---

*Generated 2026-10-28 from the implemented code in `hisi/api/patient.py` (branch `hisi_onboarding`). Next steps: resolve open items 1–2, reconcile the settings doctype JSON, then resume the Patient Journey spec if/when the user green-lights it.*
