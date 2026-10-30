# Patient Flow API — Feature Specification (BMAD)

| | |
|---|---|
| **Feature** | `patient-flow` (patient → CR ID → eligibility → consent → records) |
| **App** | `hisi` (module `Hisi`) |
| **Status** | v1.2 — implemented & live-verified 2026-10-29 (`hisi-local`); `records` step (AWS HealthLake FHIR) added as the 4th flow step (commit `945166f`); settings JSON synced |
| **Date** | 2026-10-28 |
| **Updated** | 2026-10-29 — documents the `records` step, the consent `visit_id` filter, and the synced Patient Records settings tab |
| **Requested by** | irad (irad@tiberbu.com) |
| **Implementation** | `hisi/api/patient.py` → `patient_flow` (whitelisted `hisi/api/patient.patient_flow`), tests `hisi/tests/test_patient.py` |
| **Related** | Patient Journey spec (14-dimension, DRAFT, shelved) — this flow is the first vertical slice of it |

---

## 1. Summary

One whitelisted API — `patient_flow` — executes a **frontend-controlled sequence flow** over the DHA upstream services and the AWS HealthLake FHIR datastore. The caller passes which steps to run, in what order, plus the filters each step needs:

- `patient` — fetch the client from the Client Registry (production base) → resolves the **CR ID**.
- `eligibility` — validate coverage for the CR ID (production base, POST).
- `consent` — fetch `Visit Consent` records (always runs against the UAT origin, `uat_base_url`; the only step that uses the UAT origin).
- `records` — fetch the FHIR R4 clinical resources for a visit from the **AWS HealthLake** datastore (`Encounter` by `episode-of-care`, plus the configured `_include`/`_revinclude` params), parsed from the FHIR bundle and returned as-is.

Every tunable (base URLs, URIs, timeout, JWT TTL, AWS credentials, datastore id, query params) is read from **HISI Settings** — **no tunable constants in code** (house rule set 2026-10-28). All responses go through `hisi/api/response_handler.py` (`api_endpoint` + `APIResponse`).

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
    "steps": ["patient", "eligibility", "consent", "records"],
    "filters": { "identification_type": "National ID", "identification_number": "45887531" }
  }
}
```

`data` is passed as a real JSON object. (For form-encoded REST callers the same value may arrive as a JSON **string** — `"{\"steps\": ...}"` — the endpoint parses both identically.)

### 3.1 `steps` (presence + sequence)

- Any non-empty subset/order of `patient`, `eligibility`, `consent`, `records`; the **list order is the execution order**.
- Duplicates are deduped (first occurrence wins); unknown step name → `400 BAD_REQUEST`.
- Dependency rule: if `patient` is absent, `cr_id` (or `client`) **must** be present in `filters` — the CR ID cannot be resolved otherwise.
- `records` additionally needs a visit id: from the `visit_id` filter, else from the **first consent record with a non-empty `visit_id`** when the `consent` step runs earlier in the same flow; neither source → `400 BAD_REQUEST`.

### 3.2 `filters` (per step)

| Step | Required filters | Optional filters |
|---|---|---|
| `patient` | `identification_type` + `identification_number` (both non-empty) → payload `{"identification_type","identification_number"}`; or `cr_id`/`client` → payload `{"id": <cr>}` (live-verified — DHA does NOT index CR numbers under any `identification_type` value) | — |
| `eligibility` | `cr_id`/`client` when `patient` step absent | body is fixed `{"requestIdType": <int>, "requestIdNumber": "<str>"}` — explicit `requestIdType`/`requestIdNumber` filters win; else the searched identifier (type+number) mapped via `REQUEST_ID_TYPES` (CR ID → 3); else CR ID (3) + `cr_id`; unmapped type → `400` |
| `consent` | `cr_id`/`client` when `patient` step absent | — (client filter is always `[["client", "=", <cr_id>]]`) |
| `records` | `visit_id` (non-empty), **or** the `consent` step present in the same flow (visit id taken from the first consent record that carries one) | — (nothing else is configurable — see §6, Records) |

### 3.3 Examples (live-verified 2026-10-28, unless noted)

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

Records via consent (consent supplies the visit id — test-verified; live HealthLake call pending, §9.4):

```json
{"steps": ["consent", "records"], "filters": {"cr_id": "CR2473157541615-8"}}
```

Records direct (visit id known):

```json
{"steps": ["records"], "filters": {"visit_id": "b92d1e8f-accc-48d4-84dd-2eb3e0d9fdca"}}
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
        "client_status": "",
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
      },
      "records": {
        "visit_id": "b92d1e8f-accc-48d4-84dd-2eb3e0d9fdca",
        "records": [
          { "resourceType": "Encounter", "id": "…", "status": "finished", "…": "raw FHIR R4 resource, passed through unmodified" }
        ]
      }
    },
    "steps": ["patient", "eligibility", "consent", "records"],
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
- `patient.name` = `first_name last_name` (middle name excluded from the displayed name, 2026-10-28; fallback `full_name`/`name`); `cr_id` = the fetched client's `id` (key order `CR_ID_KEYS` starts with `id`; same value at top level and in `results.patient.cr_id`).
- `patient.client_status` is returned as-is from the register, **but the live DHA CR record does not carry this field** (live probe 2026-10-28) — for real data it is therefore `""` until DHA provides it. The live record does carry `is_alive` (1/0), which is deliberately not exposed in this slice.
- `eligibility` parses DHA's `statusCode`/`statusDesc` + `schemes[]`: `eligible` = at least one scheme with `coverage.status` `"1"` (or a "covered" message); `scheme` = first covered scheme name; `schemes[]` carries the per-scheme coverage detail.
- `consent.base_url` = the UAT origin actually used; `simulation` = value of `enable_uat_simulation`.
- `consent.records` **drops consent records without a non-empty `visit_id`** (they cannot drive the records step) — test-verified 2026-10-28 (`test_consent_records_without_visit_id_filtered`).
- `records.records` = the FHIR bundle's `entry[*].resource` values, **passed through unmodified** (a bare `resource` object is accepted as a single-element list); `records.visit_id` echoes the visit id the fetch used.
- An upstream failure on **any** requested step aborts the flow with the error envelope (no partial results in v1).

### 4.2 Error envelope (via `response_handler`)

```json
{ "status": "error", "code": "BAD_REQUEST", "message": "HISI Settings uat_base_url is not configured.", "errors": [] }
```

| HTTP | code | When |
|---|---|---|
| 400 | `BAD_REQUEST` | missing/empty `data`, unknown step, empty `steps`, malformed JSON, missing patient filters, `cr_id` filter missing without `patient` step, unset `uat_base_url` / `fetch_patient_consent` / `base_url` / `fetch_client` / `eligibility_check` / consumer credentials / `aws_access_key` / `aws_secret_key` / `aws_region` / `data_store_id`, `records` step with neither a `visit_id` filter nor a consent-derived visit id |
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
| `patient_records_tab` | Tab Break | — | "Patient Records" tab (added to the repo JSON 2026-10-28) |
| `data_store_id` | Data | records | HealthLake datastore id (path `/datastore/{id}/r4/Encounter`) |
| `aws_access_key` | Data | records | AWS access key for SigV4 |
| `aws_secret_key` | Data | records | AWS secret key for SigV4 — **`Data` field, read via plain `get()`** (not `get_password`); see §7.5 and §9.5 |
| `aws_region` | Data | records | AWS region; endpoint host derived as `healthlake.{region}.amazonaws.com` (AWS global endpoint pattern — the one URL constructed in code, from a setting) |
| `query_mapper` | Table → `SHR Query Mapping` | records | child rows (`param_key`, `param_value`) appended verbatim as extra query params on every FHIR request |

House rule: **no tunable constant may be declared in code** — every endpoint/URL/TTL lives on HISI Settings; unset required values fail fast with a named `BAD_REQUEST`. (The HealthLake host is the exception in form, not in spirit: it is the AWS-global `healthlake.{aws_region}.amazonaws.com` pattern, still driven entirely by the `aws_region` setting.)

## 6. Upstream DHA contracts (live-verified 2026-10-28)

- **Auth** — self-signed **HS256 JWT** (not OAuth): header `{"alg":"HS256","typ":"JWT"}`, payload `{"key": <consumer_key>, "iat": now, "exp": now + jwt_ttl_s}`, signature `HMAC-SHA256(base64url(header)+"."+base64url(payload), consumer_secret)` → `Authorization: Bearer <jwt>`. (Reference: user's Postman pre-request script.)
- **Patient** — `GET {base_url}{fetch_client}?payload=<json>` (JSON string param). Payload shapes (live-verified 2026-10-28): `{"identification_type","identification_number"}` (e.g. National ID) or **`{"id": "<record id>"}`** — the record's `id` IS the CR string (e.g. `CR6839901180428-3`); CR numbers return 0 records under `identification_type` "CR ID" / "CR Number" / "CR". Response `{"status":200,"message":"Client successfully fetched","count":N,"data":[...]}` (the `{"message":{"total","result":[]}}` variant is parsed as well); `cr_id` may be null — `id` is the authoritative client reference.
- **Eligibility** — `POST {base_url}{eligibility_check}` (GET 404s; route confirmed). **Body schema pinned 2026-10-28** from the user's working Postman call: `{"requestIdType": 3, "requestIdNumber": "<id>"}` — `requestIdType` 3 = CR ID (confirmed live); the full `requestIdType` table (National ID, ...) is still pending from the user's Postman/DHA docs. All other probed shapes (identification_type/identification_number, cr_id/client, payload/data wraps) returned `400 "Invalid request content"`. **Response shape confirmed live 2026-10-28** (CR `CR6839901180428-3`): `{requestIdType, requestIdNumber, memberCrNumber, fullName, statusCode, statusDesc, schemes[{schemeName, schemeId, memberType, policy{startDate, endDate, number}, coverage{startDate, endDate, message, reason, status}, principalContributor{idNumber, idType, crNumber, name, employmentType, employerDetails{...}}}]}`; `coverage.status "1"` = covered (e.g. `statusCode "10"` = "Member found. Check Schemes for coverages"). The code maps the searched identifier via `REQUEST_ID_TYPES`, accepts explicit `requestIdType`/`requestIdNumber` filters as an override, and fails fast (400) on unmapped types.
- **Consent** — `GET {uat_base_url}{fetch_patient_consent}?filters=<json>&fields=<json>`; `filters = [["client","=",<cr_id>]]`, `fields` = the 13 fields in `CONSENT_FIELDS` (the request contract itself, not a tunable). Unauthenticated (UAT CMP accepts without a token — live-verified 200 with real records). Records without a non-empty `visit_id` are dropped before the response.
- **Records (AWS HealthLake, not the DHA gateway)** — `GET https://healthlake.{aws_region}.amazonaws.com/datastore/{data_store_id}/r4/Encounter?episode-of-care={visit_id}` plus every `query_mapper` row appended as extra query params (live site has four rows: `_include=Encounter:service-provider`, `_revinclude=Observation:encounter`, `_revinclude=Condition:encounter`, `_revinclude=Claim:encounter`). Auth is **AWS SigV4** (not the DHA JWT): canonical request with signed headers `host;x-amz-date` and the empty-payload SHA-256 (`e3b0c4…b855`); scope `{date}/{region}/healthlake/aws4_request`. The **visit id is the only non-configurable parameter** — everything else comes from settings (patient's design ask, 2026-10-28). Response: a FHIR R4 **Bundle**; `entry[*].resource` is extracted (a bare `resource` object counts as one) and returned as-is. Test-verified (mocked HTTP); the live call is still pending (§9.4).

## 7. Security & PII

1. Session/token auth only; no `allow_guest`.
2. Consumer secret is never echoed (read via `get_password`, travels only in the JWT signature).
3. Upstream keys: DHA scopes Client Registry visibility **per consumer key** — the site key `1IB-DHABP02215` returned `total:0` for an identifier that resolved in the user's Postman; key parity between HISI Settings and the Postman environment must be confirmed (open item).
4. `msisdn`/`date_of_birth` are returned as provided by the CR; masking policy is out of scope for this slice (covered by the Patient Journey spec when it resumes).
5. AWS signing: the SigV4 signature is computed server-side from `aws_secret_key` and never travels in the URL, body, or response. Note: `aws_secret_key` is currently a **`Data`** field (plain `get()` read) — promoting it to `Password` + `get_password()` is open item §9.5.

## 8. Test & verification status

- `hisi/tests/test_patient.py` — 37 tests: full sequence flow (URLs, params, Bearer presence/absence, consent params, **records step in the full flow** — FHIR bundle passthrough, `_include` in the signed query), byte-level JWT contract + signature verification, JWT TTL from settings, per-step standalone flows, dedupe, validation errors, not-found, ineligible-still-success, UAT base override, upstream 4xx/5xx/non-JSON, missing credentials/settings, consent records without `visit_id` filtered out, and `fetch_patient_records` directly (SigV4 URL/headers, bundle parse, missing AWS settings, empty visit id).
- Suite: **47/47 green** on `hisi-local` (`bench --site hisi-local run-tests --app hisi`, run 2026-10-29 — 37 in `test_patient.py` + 10 in `test_hisi_checklist_criterion_step`); ruff clean.
- Live: JWT accepted by production DHA (401 → 2xx); consent step returned a real record (`CR2473157541615-8`, Approved/OTP/Verified); JWT TTL default 60 confirmed. The **records** step is verified with mocked HTTP only — live HealthLake call pending (§9.4).

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
3. **Settings sync** — the Patient Records tab (`data_store_id`, `aws_access_key`, `aws_secret_key`, `aws_region`, `query_mapper` → `SHR Query Mapping`) is now in the repo `hisi_settings.json` (modified 2026-10-28 21:29 — verified); re-check that no other site-only fields remain before the next `bench migrate`.
4. **Live-verify the records step** — run `fetch_patient_records` once against the real HealthLake datastore on `hisi-local` (currently mock-verified: SigV4 URL/headers, bundle parse).
5. **`aws_secret_key` field type** — currently `Data` with a plain `get()` read; consider `Password` + `get_password()`.

---

*Generated 2026-10-28 from the implemented code in `hisi/api/patient.py` (branch `hisi_onboarding`); updated to v1.2 on 2026-10-29 to document the `records` step (AWS HealthLake FHIR, commit `945166f`), the consent `visit_id` filter, and the synced Patient Records settings tab. Next steps: resolve open items 1–2 and 4–5, then resume the Patient Journey spec if/when the user green-lights it.*
