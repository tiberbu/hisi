import base64
import ast
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
import frappe
import requests
from pydantic import BaseModel
from hisi.api.response_handler import APIResponse, BadRequest, NotFound, UpstreamError, api_endpoint

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_JWT_TTL_SECONDS = 60
CONSENT_FIELDS = [
    "name",
    "client",
    "principal_cr_id",
    "status",
    "consent_status",
    "channel",
    "verification_status",
    "verified_by",
    "verification_timestamp",
    "facility_id",
    "requesting_system",
    "visit_id",
    "visit_request_id",
    "creation",
]
CLAIM_FIELDS = [
    "name",
    "client",
    "client_name",
    "facility",
    "creation",
    "claim_status",
    "interventions",
    "interventions_code",
    "date_start",
    "date_end",
    "claim_subtype",
    "claim_amount",
    "diagnoses_code",
    "diagnoses",
    "facility_name",
    "use",
    "bundle_id",
]
PROCEDURE_FIELDS = [
    "name",
    "procedure_fulfil_status",
    "estimated_procedure_start_date",
    "estimated_procedure_end_date",
    "practitioner",
    "practitioner_name",
]
STEP_PATIENT = "patient"
STEP_ELIGIBILITY = "eligibility"
STEP_CONSENT = "consent"
STEP_RECORDS = "records"
STEP_CONTRIBUTIONS = "contributions"
STEP_CLAIMS = "claims"
STEP_PREAUTH = "preauth"
KNOWN_STEPS = (STEP_PATIENT, STEP_ELIGIBILITY, STEP_CONSENT, STEP_RECORDS, STEP_CONTRIBUTIONS, STEP_CLAIMS, STEP_PREAUTH)
REQUEST_ID_TYPES = {"CR ID": 3}
CR_ID_KEYS = ("id","cr_id", "cr_number", "client_id", "identification_number")
STATUS_KEYS = ("status", "eligibility_status", "scheme_status", "coverage_status")
SCHEME_KEYS = ("scheme", "scheme_name", "plan", "plan_name")
ELIGIBLE_STATUSES = ("active", "eligible", "valid", "covered", "in-coverage")
COVERAGE_ACTIVE_STATUS = "1"

class PatientFlowData(BaseModel):
    results: dict
    steps: list
    cr_id: str | None
    # Per-step failures (step name -> reason). A failed step never sinks the
    # other steps: the response succeeds with the steps that worked in
    # `results` and the failed ones listed here.
    errors: dict = {}

def settings_doc():
    return frappe.get_doc("HISI Settings")

def join_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + ("/" + path.lstrip("/") if path else "")

def simulation_base_url() -> str:
    value = (settings_doc().get("uat_base_url") or "").strip()
    if not value:
        raise BadRequest("HISI Settings uat_base_url is not configured.")
    return value

def dha_origin() -> str:
    """DHA origin for the active mode: the UAT base when simulation mode is
    enabled, otherwise the production base_url. The auth headers are
    unchanged by the switch — UAT and production use the same credentials."""
    doc = settings_doc()
    if doc.get("enable_uat_simulation"):
        return simulation_base_url()
    origin = (doc.get("base_url") or "").strip()
    if not origin:
        raise BadRequest("HISI Settings base_url is not configured.")
    return origin

def upstream_timeout_seconds() -> int:
    value = settings_doc().get("upstream_api_timeout")
    return int(value) if value else DEFAULT_TIMEOUT_SECONDS

def jwt_ttl_seconds() -> int:
    value = settings_doc().get("jwt_ttl_s")
    return int(value) if value else DEFAULT_JWT_TTL_SECONDS

def empty_payload_hash() -> str:
    value = (settings_doc().get("empty_payload_sha256") or "").strip()
    return value or EMPTY_PAYLOAD_SHA256

def consent_resource_path() -> str:
    value = (settings_doc().get("fetch_patient_consent") or "").strip()
    if not value:
        raise BadRequest("HISI Settings fetch_patient_consent is not configured.")
    return value

def med_claim_search_path() -> str:
    value = (settings_doc().get("med_claim_search") or "").strip()
    if not value:
        raise BadRequest("HISI Settings med_claim_search is not configured.")
    return value

def med_preauth_search_path() -> str:
    value = (settings_doc().get("med_preauth_search") or "").strip()
    if not value:
        raise BadRequest("HISI Settings med_preauth_search is not configured.")
    return value

def med_preauth_procedure_search_path() -> str:
    value = (settings_doc().get("med_preauth_procedure_search") or "").strip()
    if not value:
        raise BadRequest("HISI Settings med_preauth_procedure_search is not configured.")
    return value

def bmx_intervention_search_path() -> str:
    value = (settings_doc().get("bmx_intervention_search") or "").strip()
    if not value:
        raise BadRequest("HISI Settings bmx_intervention_search is not configured.")
    return value

def timeline_data_source() -> str:
    # API for now; the Database source (direct SHR/consent/claims queries)
    # plugs into the same steps once the connection is available.
    value = (settings_doc().get("data_source") or "API").strip().upper()
    if value != "API":
        raise BadRequest("The {} data source is not available yet — set HISI Settings data source to API.".format(value.title()))
    return value

def base64url_encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=").replace("+", "-").replace("/", "_")

def dha_jwt() -> str:
    doc = settings_doc()
    key = (doc.get("consumer_key") or "").strip()
    secret = doc.get_password("consumer_secret") or ""
    if not key or not secret:
        raise BadRequest("HISI Settings consumer_key / consumer_secret are not configured.")
    now = int(time.time())
    header = base64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    payload = base64url_encode(json.dumps({"key": key, "iat": now, "exp": now + jwt_ttl_seconds()}, separators=(",", ":")).encode("utf-8"))
    signing_input = "{}.{}".format(header, payload).encode("utf-8")
    signature = base64url_encode(hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest())
    return "{}.{}.{}".format(header, payload, signature)

def request_headers(bearer: bool = True, basic: str = "") -> dict:
    if basic:
        return {"Authorization": "Basic " + basic}
    return {"Authorization": "Bearer " + dha_jwt()} if bearer else {}

def dha_basic_credentials() -> str:
    doc = settings_doc()
    username = (doc.get("username") or "").strip()
    password = doc.get_password("password") or ""
    if not username or not password:
        raise BadRequest("HISI Settings username / password are not configured.")
    return base64.b64encode("{}:{}".format(username, password).encode("utf-8")).decode("ascii")

UPSTREAM_RETRY_ATTEMPTS = 3
UPSTREAM_RETRYABLE_STATUSES = (404, 500, 502, 503, 504)
UPSTREAM_RETRY_BACKOFF_SECONDS = 0.5

def _upstream_retry(attempt: int, response=None) -> bool:
    """Whether a failed DHA request should be retried. The UAT origin flaps
    individual routes to "404 Route Not Found" and back to 200, so a
    retryable status (404/5xx) — or a timeout — is retried with a short
    backoff before surfacing as an error. Anything else (400, 401, ...)
    fails immediately."""
    if attempt + 1 >= UPSTREAM_RETRY_ATTEMPTS:
        return False
    status = getattr(response, "status_code", None)
    if status is not None and status not in UPSTREAM_RETRYABLE_STATUSES:
        return False
    time.sleep(UPSTREAM_RETRY_BACKOFF_SECONDS * (attempt + 1))
    return True

def http_get(url: str, params: dict, bearer: bool = True, basic: str = "") -> dict:
    for attempt in range(UPSTREAM_RETRY_ATTEMPTS):
        try:
            response = requests.get(url, params=params, headers=request_headers(bearer, basic), timeout=upstream_timeout_seconds())
            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                raise UpstreamError("Upstream returned a non-JSON response.")
        except requests.exceptions.Timeout:
            if _upstream_retry(attempt):
                continue
            raise UpstreamError("Upstream request timed out.")
        except requests.exceptions.HTTPError as e:
            if _upstream_retry(attempt, e.response):
                continue
            raise UpstreamError("Upstream request failed with HTTP {}.".format(getattr(e.response, "status_code", "error")))
        except requests.exceptions.RequestException:
            raise UpstreamError("Upstream request failed.")

def http_post(url: str, body: dict, bearer: bool = True) -> dict:
    for attempt in range(UPSTREAM_RETRY_ATTEMPTS):
        try:
            response = requests.post(url, json=body, headers=request_headers(bearer), timeout=upstream_timeout_seconds())
            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                raise UpstreamError("Upstream returned a non-JSON response.")
        except requests.exceptions.Timeout:
            if _upstream_retry(attempt):
                continue
            raise UpstreamError("Upstream request timed out.")
        except requests.exceptions.HTTPError as e:
            if _upstream_retry(attempt, e.response):
                continue
            raise UpstreamError("Upstream request failed with HTTP {}.".format(getattr(e.response, "status_code", "error")))
        except requests.exceptions.RequestException:
            raise UpstreamError("Upstream request failed.")

AWS_SERVICE = "healthlake"
EMPTY_PAYLOAD_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

def aws_credentials() -> tuple:
    doc = settings_doc()
    access_key = (doc.get("aws_access_key") or "").strip()
    secret_key = (doc.get("aws_secret_key") or "").strip()
    region = (doc.get("aws_region") or "").strip()
    data_store_id = (doc.get("data_store_id") or "").strip()
    if not access_key or not secret_key or not region or not data_store_id:
        raise BadRequest("HISI Settings aws_access_key / aws_secret_key / aws_region / data_store_id are not configured.")
    return access_key, secret_key, region, data_store_id

def canonical_query_string(params: list) -> str:
    encoded = sorted((quote(str(key), safe="-_.~"), quote(str(value), safe="-_.~")) for key, value in params)
    return "&".join(key + "=" + value for key, value in encoded)

def sigv4_headers(method: str, host: str, path: str, canonical_query: str, access_key: str, secret_key: str, region: str) -> dict:
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    canonical_request = "\n".join([
        method,
        path,
        canonical_query,
        "host:{}\nx-amz-date:{}\n".format(host, amz_date),
        "host;x-amz-date",
        empty_payload_hash(),
    ])
    scope = "{}/{}/{}/aws4_request".format(date, region, AWS_SERVICE)
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    key = ("AWS4" + secret_key).encode("utf-8")
    key = hmac.new(key, date.encode("utf-8"), hashlib.sha256).digest()
    key = hmac.new(key, region.encode("utf-8"), hashlib.sha256).digest()
    key = hmac.new(key, AWS_SERVICE.encode("utf-8"), hashlib.sha256).digest()
    key = hmac.new(key, b"aws4_request", hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Authorization": "AWS4-HMAC-SHA256 Credential={}/{}, SignedHeaders=host;x-amz-date, Signature={}".format(access_key, scope, signature),
        "X-Amz-Date": amz_date,
    }

def aws_get(path: str, params: list, host: str, access_key: str, secret_key: str, region: str) -> dict:
    canonical_query = canonical_query_string(params)
    url = "https://" + host + path + ("?" + canonical_query if canonical_query else "")
    headers = sigv4_headers("GET", host, path, canonical_query, access_key, secret_key, region)
    try:
        response = requests.get(url, headers=headers, timeout=upstream_timeout_seconds())
        response.raise_for_status()
    except requests.exceptions.Timeout:
        raise UpstreamError("Upstream request timed out.")
    except requests.exceptions.HTTPError as e:
        raise UpstreamError("Upstream request failed with HTTP {}.".format(getattr(e.response, "status_code", "error")))
    except requests.exceptions.RequestException:
        raise UpstreamError("Upstream request failed.")
    try:
        return response.json()
    except ValueError:
        raise UpstreamError("Upstream returned a non-JSON response.")

def parse_fhir_bundle(payload) -> list:
    if not isinstance(payload, dict):
        return []
    entries = payload.get("entry")
    if isinstance(entries, list):
        records = [entry["resource"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)]
        if records:
            return records
    resource = payload.get("resource")
    return [resource] if isinstance(resource, dict) else []

def _text(value) -> str:
    return str(value).strip() if value is not None else ""

def _code_or_text(node) -> tuple:
    """(code, display) from a plain string, Coding, or CodeableConcept node."""
    if isinstance(node, str):
        value = node.strip()
        return value, value
    if isinstance(node, dict):
        codings = node.get("coding")
        if isinstance(codings, list):
            for coding in codings:
                if isinstance(coding, dict) and (coding.get("code") or coding.get("display")):
                    return _text(coding.get("code")), _text(coding.get("display"))
        return "", _text(node.get("text"))
    return "", ""

def _concept_text(node) -> str:
    code, display = _code_or_text(node)
    if display and code and display.lower() != code.lower():
        return "{} ({})".format(display, code)
    return display or code

def _money(node) -> str:
    """Human money: currency first with thousands separators ("KES 134,400");
    missing or zero amounts read as "Not priced"."""
    if not isinstance(node, dict):
        return ""
    value = node.get("value")
    currency = _text(node.get("currency"))
    if value in (None, ""):
        return "Not priced"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "{} {}".format(value, currency).strip()
    if amount == 0:
        return "Not priced"
    text = "{:,.0f}".format(amount) if amount == amount.to_integral_value() else "{:,.2f}".format(amount)
    return "{} {}".format(currency, text).strip()

def _date_part(value) -> str:
    """Date portion of an ISO date/datetime (drops the time-of-day)."""
    value = _text(value)
    if len(value) >= 10 and value[4:5] == "-":
        return value[:10]
    return value

def _date_range(node) -> str:
    if not isinstance(node, dict):
        return ""
    start, end = _text(node.get("start")), _text(node.get("end"))
    if start and end and start != end:
        return "{} - {}".format(start, end)
    return start or end

def _ref_text(node) -> str:
    """Plain text for a Reference: display, then id, then the reference tail."""
    if isinstance(node, str):
        return node.split("/")[-1] if "/" in node else node
    if not isinstance(node, dict):
        return ""
    if node.get("display"):
        return _text(node.get("display"))
    if node.get("id"):
        return _text(node.get("id"))
    reference = _text(node.get("reference"))
    return reference.split("/")[-1] if reference else ""

def _append(details: list, label: str, value, sub=None) -> None:
    value = _text(value)
    if value:
        sub = _text(sub)
        details.append((label, value, sub) if sub else (label, value))

def _contribution_status_details(raw, details: list, seen: set) -> None:
    """Parse the SHA contributionStatus extension (a JSON string embedded in
    the Claim): overall status, per-scheme coverage and the principal
    contributor behind each scheme."""
    if not raw:
        return
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return
    if not isinstance(data, dict):
        return
    _append(details, "Contribution status", data.get("statusDesc"))
    for scheme in data.get("schemes") or []:
        if not isinstance(scheme, dict):
            continue
        name = _text(scheme.get("schemeName"))
        coverage = scheme.get("coverage") if isinstance(scheme.get("coverage"), dict) else {}
        policy = scheme.get("policy") if isinstance(scheme.get("policy"), dict) else {}
        covered = _text(coverage.get("status")) == COVERAGE_ACTIVE_STATUS
        bits = ["covered" if covered else "not covered"]
        reason = _text(coverage.get("reason"))
        if reason:
            bits.append(reason)
        policy_number = _text(policy.get("number"))
        if policy_number:
            bits.append("policy " + policy_number)
        policy_period = _date_range(policy)
        if policy_period:
            bits.append(policy_period)
        if name:
            details.append(("Coverage - {}".format(name), ", ".join(bits)))
        principal = scheme.get("principalContributor")
        if isinstance(principal, dict):
            principal_name = _text(principal.get("name"))
            if principal_name:
                pbits = [principal_name]
                relationship = _text(principal.get("relationship"))
                if relationship:
                    pbits.append(relationship)
                employment = _text(principal.get("employmentType"))
                if employment:
                    pbits.append(employment)
                employer = principal.get("employerDetails")
                if isinstance(employer, dict):
                    employer_name = _text(employer.get("name"))
                    if employer_name:
                        pbits.append(employer_name)
                    compliance = _text(employer.get("complianceStatus"))
                    if compliance:
                        pbits.append(compliance)
                key = " | ".join(pbits)
                if key not in seen:
                    seen.add(key)
                    details.append(("Principal contributor", key))

def _claim_kind(use_code: str) -> str:
    """Human label for Claim.use: claim, preauthorization or quote."""
    if use_code == "preauthorization":
        return "Preauthorization request"
    if use_code == "quote":
        return "Quote request"
    if use_code == "claim":
        return "Claim"
    return ""

def _visit_conditions(records: list) -> list:
    """(code, display) pairs from every Condition resource in the visit, so a
    Claim hero band can name the clinical condition behind the billing."""
    pairs = []
    for record in records:
        if not isinstance(record, dict) or _text(record.get("resourceType")) != "Condition":
            continue
        code, display = _code_or_text(record.get("code"))
        if not display and not code:
            continue
        pairs.append((code, display or code))
    return pairs

def _claim_condition(diagnosis_codes: set, conditions: list) -> tuple:
    """Best-effort link between a Claim's diagnoses and the visit's Condition
    resources: an exact ICD code match wins, a lone condition is taken as-is.
    Returns (code, display) so the UI can show the code small under the name."""
    if not conditions:
        return "", ""
    if len(conditions) == 1:
        return conditions[0]
    for code, display in conditions:
        if code and code in diagnosis_codes:
            return code, display
    return "", "; ".join(display for _, display in conditions)

def _claim_details(claim: dict, conditions: list = ()) -> tuple:
    claim_group = []
    billing_group = []
    coverage_group = []
    diagnosis_group = []
    use_code, _ = _code_or_text(claim.get("use"))
    kind = _claim_kind(use_code)
    _append(claim_group, "Kind", kind)
    _append(claim_group, "Status", claim.get("status"))
    _append(claim_group, "Claim type", _concept_text(claim.get("type")))
    _append(claim_group, "Sub-type", _concept_text(claim.get("subType")))
    _append(claim_group, "Priority", _concept_text(claim.get("priority")))
    _append(claim_group, "Created", claim.get("created"))
    _append(claim_group, "Billable period", _date_range(claim.get("billablePeriod")))
    _append(claim_group, "Insurer", _ref_text(claim.get("insurer")))
    patient_ref = claim.get("patient")
    patient_id = _text(patient_ref.get("reference").split("/")[-1]) if isinstance(patient_ref, dict) and patient_ref.get("reference") else ""
    _append(claim_group, "Patient", _ref_text(claim.get("patient")), patient_id)
    _append(claim_group, "Provider", _ref_text(claim.get("provider")))
    total = _money(claim.get("total"))
    for extension in claim.get("extension") or []:
        if not isinstance(extension, dict):
            continue
        url = _text(extension.get("url"))
        if url.endswith("extension-patientInvoice"):
            for child in extension.get("extension") or []:
                if not isinstance(child, dict):
                    continue
                key = _text(child.get("url"))
                if key == "serviceType":
                    _append(billing_group, "Service type", child.get("valueString"))
                elif key == "invoiceNumber":
                    value = _text(child.get("valueString")).replace("Provider Invoice Number:", "").strip()
                    _append(billing_group, "Invoice number", value)
                elif key == "invoiceDate":
                    _append(billing_group, "Invoice date", child.get("valueDate"))
                elif key == "invoiceAmount":
                    _append(billing_group, "Invoice amount", _money(child.get("valueMoney")))
                elif key.endswith("extension-patient-share"):
                    _append(billing_group, "Patient share", _money(child.get("valueMoney")))
        elif url.endswith("contributionStatus"):
            _contribution_status_details(extension.get("valueString"), coverage_group, set())
    diagnosis_bits = []
    diagnosis_codes = set()
    for diagnosis in claim.get("diagnosis") or []:
        if not isinstance(diagnosis, dict):
            continue
        code, display = _code_or_text(diagnosis.get("diagnosisCodeableConcept"))
        if code:
            diagnosis_codes.add(code)
        if display:
            diagnosis_bits.append(display)
        code_text = _concept_text(diagnosis.get("diagnosisCodeableConcept"))
        if code_text:
            sequence = diagnosis.get("sequence")
            _append(diagnosis_group, "Diagnosis #{}".format(sequence) if sequence else "Diagnosis", code_text)
    items = claim.get("item") or []
    line_rows = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        description_bits = []
        category_display = _code_or_text(item.get("category"))[1]
        if category_display:
            description_bits.append(category_display)
        service = _concept_text(item.get("productOrService"))
        if service:
            description_bits.append(service)
        quantity = item.get("quantity")
        if isinstance(quantity, dict) and quantity.get("value") not in (None, ""):
            description_bits.append("{} x".format(quantity.get("value")))
        line_rows.append([
            str(index),
            "; ".join(description_bits) or "-",
            _money(item.get("unitPrice")) or "-",
            _money(item.get("net")) or "-",
        ])
    _append(billing_group, "Total", total)
    headline = []
    if diagnosis_bits:
        headline.append(("Diagnoses", "; ".join(diagnosis_bits), "; ".join(sorted(diagnosis_codes))))
    billable = claim.get("billablePeriod")
    if isinstance(billable, dict):
        billable = {"start": _date_part(billable.get("start")), "end": _date_part(billable.get("end"))}
    _append(headline, "Service period", _date_range(billable))
    _append(headline, "Amount", total)
    condition_code, condition_display = _claim_condition(diagnosis_codes, conditions)
    _append(headline, "Condition", condition_display, condition_code)
    summary_bits = [bit for bit in (kind, _text(claim.get("status")), "total " + total if total and total != "Not priced" else "") if bit]
    groups = [("Claim", claim_group), ("Billing", billing_group, {
        "columns": ["#", "Item", "Unit price", "Net"],
        "rows": line_rows,
    }), ("Coverage", coverage_group), ("Diagnoses", diagnosis_group)]
    return headline, groups, ", ".join(summary_bits)

def _encounter_details(encounter: dict) -> tuple:
    visit_group = []
    people_group = []
    note_group = []
    status = _text(encounter.get("status"))
    klass = _concept_text(encounter.get("class"))
    period = _date_range(encounter.get("period"))
    provider = _ref_text(encounter.get("serviceProvider"))
    headline = []
    for label, value in (("Class", klass), ("Status", status), ("Period", period), ("Provider", provider)):
        if value:
            headline.append((label, value))
    _append(visit_group, "Class", klass)
    _append(visit_group, "Status", status)
    _append(visit_group, "Period", period)
    locations = []
    for location in encounter.get("location") or []:
        if isinstance(location, dict):
            name = _ref_text(location.get("location"))
            if name:
                locations.append(name)
    _append(visit_group, "Location", "; ".join(locations))
    subject_ref = encounter.get("subject")
    subject_id = _text(subject_ref.get("reference").split("/")[-1]) if isinstance(subject_ref, dict) and subject_ref.get("reference") else ""
    _append(people_group, "Patient", _ref_text(encounter.get("subject")), subject_id)
    _append(people_group, "Provider", provider)
    participants = []
    for participant in encounter.get("participant") or []:
        if isinstance(participant, dict):
            individual = _ref_text(participant.get("individual"))
            if individual:
                participants.append(individual)
    _append(people_group, "Participants", "; ".join(participants))
    for note in encounter.get("note") or []:
        if isinstance(note, dict):
            _append(note_group, "Note", note.get("text"))
    groups = [("Visit", visit_group), ("People", people_group), ("Notes", note_group)]
    return headline, groups, ", ".join(bit for bit in (klass, status, period) if bit)

def _condition_details(condition: dict) -> tuple:
    diagnosis_group = []
    status_group = []
    diagnosis_code, diagnosis_display = _code_or_text(condition.get("code"))
    diagnosis = _concept_text(condition.get("code"))
    clinical = _concept_text(condition.get("clinicalStatus"))
    onset = _text(condition.get("onsetDateTime")) or _date_range(condition.get("onsetPeriod"))
    headline = []
    for label, value in (("Diagnosis", diagnosis), ("Clinical status", clinical), ("Onset", onset)):
        if value:
            headline.append((label, value, diagnosis_code) if label == "Diagnosis" else (label, value))
    _append(diagnosis_group, "Diagnosis", diagnosis, diagnosis_code)
    categories = []
    for category in condition.get("category") or []:
        text = _concept_text(category)
        if text:
            categories.append(text)
    _append(diagnosis_group, "Category", ", ".join(categories))
    _append(diagnosis_group, "Onset", onset)
    _append(diagnosis_group, "Recorded", condition.get("recordedDate"))
    _append(status_group, "Clinical status", clinical)
    _append(status_group, "Verification", _concept_text(condition.get("verificationStatus")))
    _append(status_group, "Encounter", _ref_text(condition.get("encounter")))
    groups = [("Diagnosis", diagnosis_group), ("Status", status_group)]
    return headline, groups, ", ".join(bit for bit in (diagnosis, clinical) if bit)

def _organization_details(org: dict) -> tuple:
    identity_group = []
    contact_group = []
    name = _text(org.get("name"))
    types = []
    for entry in org.get("type") or []:
        if isinstance(entry, dict):
            text = _concept_text(entry)
            if text:
                types.append(text)
    type_text = ", ".join(types)
    active = ""
    if org.get("active") is not None:
        active = "Yes" if org.get("active") else "No"
    headline = []
    for label, value in (("Name", name), ("Type", type_text), ("Active", active)):
        if value:
            headline.append((label, value))
    _append(identity_group, "Name", name)
    _append(identity_group, "Type", type_text)
    _append(identity_group, "Active", active)
    addresses = []
    for address in org.get("address") or []:
        if isinstance(address, dict):
            parts = [_text(address.get(key)) for key in ("city", "district", "state", "postalCode", "country")]
            line = ", ".join(part for part in parts if part)
            if line:
                addresses.append(line)
    _append(contact_group, "Address", "; ".join(addresses))
    contacts = []
    for telecom in org.get("telecom") or []:
        if isinstance(telecom, dict) and _text(telecom.get("value")):
            system = _text(telecom.get("system"))
            value = _text(telecom.get("value"))
            contacts.append("{}: {}".format(system, value) if system else value)
    _append(contact_group, "Contact", "; ".join(contacts))
    groups = [("Identity", identity_group), ("Contact", contact_group)]
    return headline, groups, ", ".join(bit for bit in (name, type_text) if bit)

def _medication_request_details(request: dict) -> tuple:
    prescription_group = []
    status_group = []
    medication = _concept_text(request.get("medicationCodeableConcept")) or _ref_text(request.get("medicationReference"))
    status = _text(request.get("status"))
    dosages = []
    for instruction in request.get("dosageInstruction") or []:
        if isinstance(instruction, dict):
            text = _text(instruction.get("text"))
            if text:
                dosages.append(text)
    dose = "; ".join(dosages)
    headline = []
    for label, value in (("Medication", medication), ("Status", status), ("Dose", dose)):
        if value:
            headline.append((label, value))
    _append(prescription_group, "Medication", medication)
    _append(prescription_group, "Dose", dose)
    _append(status_group, "Status", status)
    _append(status_group, "Intent", request.get("intent"))
    _append(status_group, "Authored", request.get("authoredOn"))
    _append(status_group, "Requester", _ref_text(request.get("requester")))
    groups = [("Prescription", prescription_group), ("Status", status_group)]
    return headline, groups, ", ".join(bit for bit in (medication, status) if bit)

def _observation_details(observation: dict) -> tuple:
    measurement_group = []
    timing_group = []
    code = _concept_text(observation.get("code"))
    value_quantity = observation.get("valueQuantity")
    if isinstance(value_quantity, dict) and value_quantity.get("value") not in (None, ""):
        unit = _text(value_quantity.get("unit"))
        value = "{} {}".format(value_quantity.get("value"), unit).strip()
    else:
        value = _text(observation.get("valueString")) or _concept_text(observation.get("valueCodeableConcept"))
    effective = _text(observation.get("effectiveDateTime")) or _date_range(observation.get("effectivePeriod"))
    headline = []
    for label, val in (("Observation", code), ("Value", value), ("Effective", effective)):
        if val:
            headline.append((label, val))
    _append(measurement_group, "Observation", code)
    _append(measurement_group, "Value", value)
    _append(timing_group, "Status", observation.get("status"))
    _append(timing_group, "Effective", effective)
    _append(timing_group, "Issued", observation.get("issued"))
    groups = [("Measurement", measurement_group), ("Timing", timing_group)]
    return headline, groups, ", ".join(bit for bit in (code, value) if bit)

def _service_request_details(request: dict) -> tuple:
    request_group = []
    status_group = []
    service = _concept_text(request.get("code"))
    status = _text(request.get("status"))
    occurrence = _text(request.get("occurrenceDateTime")) or _date_range(request.get("occurrencePeriod"))
    headline = []
    for label, value in (("Service", service), ("Status", status), ("Occurrence", occurrence)):
        if value:
            headline.append((label, value))
    _append(request_group, "Service", service)
    _append(request_group, "Occurrence", occurrence)
    _append(status_group, "Intent", request.get("intent"))
    _append(status_group, "Status", status)
    _append(status_group, "Authored", request.get("authoredOn"))
    _append(status_group, "Requester", _ref_text(request.get("requester")))
    groups = [("Request", request_group), ("Status", status_group)]
    return headline, groups, ", ".join(bit for bit in (service, status) if bit)

def _procedure_details(procedure: dict) -> tuple:
    procedure_group = []
    status_group = []
    code = _concept_text(procedure.get("code"))
    status = _text(procedure.get("status"))
    performed = _text(procedure.get("performedDateTime")) or _date_range(procedure.get("performedPeriod"))
    headline = []
    for label, value in (("Procedure", code), ("Status", status), ("Performed", performed)):
        if value:
            headline.append((label, value))
    _append(procedure_group, "Procedure", code)
    _append(procedure_group, "Performed", performed)
    performers = []
    for performer in procedure.get("performer") or []:
        if isinstance(performer, dict):
            actor = _ref_text(performer.get("actor"))
            if actor:
                performers.append(actor)
    _append(procedure_group, "Performed by", ", ".join(performers))
    _append(status_group, "Status", status)
    groups = [("Procedure", procedure_group), ("Status", status_group)]
    return headline, groups, ", ".join(bit for bit in (code, status) if bit)

def _diagnostic_report_details(report: dict) -> tuple:
    report_group = []
    status_group = []
    code = _concept_text(report.get("code"))
    status = _text(report.get("status"))
    conclusion = _text(report.get("conclusion"))
    headline = []
    for label, value in (("Report", code), ("Status", status), ("Conclusion", conclusion)):
        if value:
            headline.append((label, value))
    _append(report_group, "Report", code)
    categories = []
    for category in report.get("category") or []:
        text = _concept_text(category)
        if text:
            categories.append(text)
    _append(report_group, "Category", ", ".join(categories))
    _append(report_group, "Conclusion", conclusion)
    _append(status_group, "Status", status)
    _append(status_group, "Issued", report.get("issued"))
    groups = [("Report", report_group), ("Status", status_group)]
    return headline, groups, ", ".join(bit for bit in (code, status) if bit)

def _document_reference_details(document: dict) -> tuple:
    document_group = []
    status_group = []
    title = _text(document.get("title"))
    doc_type = _concept_text(document.get("type"))
    status = _text(document.get("status"))
    headline = []
    for label, value in (("Title", title), ("Type", doc_type), ("Status", status)):
        if value:
            headline.append((label, value))
    _append(document_group, "Title", title)
    _append(document_group, "Type", doc_type)
    _append(document_group, "Description", document.get("description"))
    _append(status_group, "Status", status)
    _append(status_group, "Created", document.get("created"))
    groups = [("Document", document_group), ("Status", status_group)]
    return headline, groups, ", ".join(bit for bit in (title, doc_type) if bit)

def _allergy_details(allergy: dict) -> tuple:
    allergy_group = []
    status_group = []
    substance = _concept_text(allergy.get("code"))
    clinical = _concept_text(allergy.get("clinicalStatus"))
    criticality = _text(allergy.get("criticality"))
    headline = []
    for label, value in (("Substance", substance), ("Clinical status", clinical), ("Criticality", criticality)):
        if value:
            headline.append((label, value))
    _append(allergy_group, "Substance", substance)
    categories = []
    for category in allergy.get("category") or []:
        text = _text(category)
        if text:
            categories.append(text)
    _append(allergy_group, "Category", ", ".join(categories))
    _append(allergy_group, "Criticality", criticality)
    _append(status_group, "Clinical status", clinical)
    groups = [("Allergy", allergy_group), ("Status", status_group)]
    return headline, groups, ", ".join(bit for bit in (substance, clinical) if bit)

def _coverage_details(coverage: dict) -> tuple:
    coverage_group = []
    status_group = []
    name = _text(coverage.get("name"))
    status = _text(coverage.get("status"))
    payors = []
    for payor in coverage.get("payor") or []:
        if isinstance(payor, dict):
            payor_name = _ref_text(payor.get("reference"))
            if payor_name:
                payors.append(payor_name)
    payor_text = ", ".join(payors)
    headline = []
    for label, value in (("Name", name), ("Status", status), ("Payor", payor_text)):
        if value:
            headline.append((label, value))
    _append(coverage_group, "Name", name)
    _append(coverage_group, "Subscriber", _ref_text(coverage.get("subscriber")))
    _append(coverage_group, "Beneficiary", _ref_text(coverage.get("beneficiary")))
    _append(coverage_group, "Payor", payor_text)
    _append(status_group, "Status", status)
    _append(status_group, "Period", _date_range(coverage.get("period")))
    groups = [("Coverage", coverage_group), ("Status", status_group)]
    return headline, groups, ", ".join(bit for bit in (name, status) if bit)

FHIR_DETAIL_EXTRACTORS = {
    "Claim": _claim_details,
    "Encounter": _encounter_details,
    "Condition": _condition_details,
    "Organization": _organization_details,
    "MedicationRequest": _medication_request_details,
    "Observation": _observation_details,
    "ServiceRequest": _service_request_details,
    "Procedure": _procedure_details,
    "DiagnosticReport": _diagnostic_report_details,
    "DocumentReference": _document_reference_details,
    "AllergyIntolerance": _allergy_details,
    "Coverage": _coverage_details,
}

def enrich_fhir_records(records: list) -> list:
    """Attach a curated `headline`, a grouped `groups` tree and a one-line
    `summary` to every raw FHIR resource so the UI can layer the data:
    key facts up front, full detail in collapsible groups."""
    conditions = _visit_conditions(records)
    enriched = []
    for resource in records:
        if not isinstance(resource, dict):
            continue
        record = dict(resource)
        extractor = FHIR_DETAIL_EXTRACTORS.get(_text(resource.get("resourceType")))
        headline, groups, summary = [], [], ""
        if extractor:
            if _text(resource.get("resourceType")) == "Claim":
                headline, groups, summary = extractor(resource, conditions)
            else:
                headline, groups, summary = extractor(resource)
        meta = resource.get("meta")
        if isinstance(meta, dict) and meta.get("lastUpdated"):
            groups.append(("Metadata", [("Last updated", meta.get("lastUpdated"))]))
        if headline:
            record["headline"] = [
                {"label": entry[0], "value": entry[1], **({"sub": entry[2]} if len(entry) > 2 and entry[2] else {})}
                for entry in headline
            ]
        if groups:
            serialized_groups = []
            for group in groups:
                title = group[0]
                items = group[1]
                table = group[2] if len(group) > 2 and isinstance(group[2], dict) else None
                if not items and not table:
                    continue
                serialized = {
                    "title": title,
                    "items": [
                        {"label": entry[0], "value": entry[1], **({"sub": entry[2]} if len(entry) > 2 and entry[2] else {})}
                        for entry in items
                    ],
                }
                if table:
                    serialized["table"] = table
                serialized_groups.append(serialized)
            record["groups"] = serialized_groups
        if summary:
            record["summary"] = summary
        enriched.append(record)
    return enriched

def _encounter_facility_refs(encounter: dict) -> list:
    """The facility references an Encounter reveals: its serviceProvider plus
    any location[].organization, as raw reference strings (the reference is
    fetched by id, since the visit-scoped search returns only the reference)."""
    refs = []
    provider = encounter.get("serviceProvider")
    if isinstance(provider, dict) and _text(provider.get("reference")):
        refs.append(provider["reference"])
    for location in encounter.get("location") or []:
        org = location.get("organization") if isinstance(location, dict) else None
        if isinstance(org, dict) and _text(org.get("reference")):
            refs.append(org["reference"])
    return refs

def fetch_organization_by_id(org_id: str, host: str, access_key: str, secret_key: str, region: str, data_store_id: str) -> dict:
    """One Organization resource by id; the facility name and type live on
    the resource itself. The by-id GET returns a bare resource (not a
    bundle); an empty dict means the reference could not be resolved."""
    path = "/datastore/{}/r4/Organization/{}".format(data_store_id, org_id)
    payload = aws_get(path, [], host, access_key, secret_key, region)
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("resource"), dict):
        return payload["resource"]
    return payload if _text(payload.get("resourceType")) == "Organization" else {}

def fetch_patient_records(visit_id: str, org_cache: dict = None) -> list:
    visit_id = str(visit_id or "").strip()
    if not visit_id:
        raise BadRequest("A visit_id is required to fetch patient records.")
    access_key, secret_key, region, data_store_id = aws_credentials()
    host = "healthlake.{}.amazonaws.com".format(region)
    path = "/datastore/{}/r4/Encounter".format(data_store_id)
    params = [("episode-of-care", visit_id)]
    for row in settings_doc().get("query_mapper") or []:
        key = str(row.get("param_key") or "").strip()
        if key:
            params.append((key, str(row.get("param_value") or "").strip()))
    records = parse_fhir_bundle(aws_get(path, params, host, access_key, secret_key, region))
    # The visit-scoped search returns the Encounter with only a facility
    # reference; resolve it to the full Organization so the visit's bucket
    # carries the facility the journey UI renders (and the ambulance on an
    # EMS encounter, which the UI reports as the responding unit).
    cache = org_cache if org_cache is not None else {}
    facility = []
    added = set()
    for record in records:
        if not isinstance(record, dict) or _text(record.get("resourceType")) != "Encounter":
            continue
        for reference in _encounter_facility_refs(record):
            org_id = _visit_reference_id(reference)
            if not org_id or org_id in added:
                continue
            if org_id not in cache:
                try:
                    cache[org_id] = fetch_organization_by_id(org_id, host, access_key, secret_key, region, data_store_id)
                except UpstreamError:
                    cache[org_id] = {}
            if cache[org_id]:
                added.add(org_id)
                facility.append(cache[org_id])
    return enrich_fhir_records(records + facility)

def mpi_id_resolution_path() -> str:
    value = (settings_doc().get("mpi_id_resolution") or "").strip()
    if not value:
        raise BadRequest("HISI Settings mpi_id_resolution is not configured.")
    return value

def parse_mpi_patient(payload) -> dict:
    """MPI patient envelope: data.patient.data holds cr_id / mpi_id / shr_id."""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        patient = data.get("patient")
        if isinstance(patient, dict) and isinstance(patient.get("data"), dict):
            return patient["data"]
    return {}

def resolve_shr_patient_id(cr_id: str) -> str:
    """CR ID -> SHR patient id via the configured DHA MPI resolution endpoint;
    the SHR id keys the patient-level FHIR fetches (subject/patient refs).
    UAT simulation mode routes the call to the UAT origin, otherwise the
    production base_url; the call is authorized with HTTP Basic
    (settings username/password) — the MPI endpoints reject the JWT bearer."""
    cr_id = str(cr_id or "").strip()
    if not cr_id:
        return ""
    origin = dha_origin()
    record = parse_mpi_patient(http_get(join_url(origin, mpi_id_resolution_path()), {"cr_id": cr_id}, basic=dha_basic_credentials()))
    return str(record.get("shr_id") or "").strip()

def fetch_patient_level_records(shr_id: str, cr_id: str = "") -> tuple:
    """Patient-level FHIR fetches keyed on the patient reference rather than a
    visit id: one signed search per configured resource type and per
    identifier value (the resolved SHR id plus the CR id as-is, so records
    keyed on either identifier are found). The param name (subject/patient/...)
    varies per resource row."""
    access_key, secret_key, region, data_store_id = aws_credentials()
    host = "healthlake.{}.amazonaws.com".format(region)
    values = []
    for value in (str(shr_id or "").strip(), str(cr_id or "").strip()):
        if value and value not in values:
            values.append(value)
    by_resource = {}
    errors = {}
    for row in settings_doc().get("patient_resources") or []:
        param_key = str(row.get("patient_param") or "").strip() or "subject"
        resource_types = [
            part.strip() for part in str(row.get("resource_type") or "").replace("\n", ",").split(",") if part.strip()
        ]
        for resource_type in resource_types:
            path = "/datastore/{}/r4/{}".format(data_store_id, resource_type)
            for value in values:
                try:
                    records = parse_fhir_bundle(aws_get(path, [(param_key, value)], host, access_key, secret_key, region))
                except UpstreamError as exc:
                    errors[resource_type] = str(exc)
                    continue
                by_resource.setdefault(resource_type, []).extend(records)
    return by_resource, errors

def _record_dedupe_key(record: dict) -> tuple:
    return (_text(record.get("resourceType")), _text(record.get("id")))

def parse_records(payload) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "result", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    message = payload.get("message")
    if isinstance(message, list):
        return message
    if isinstance(message, dict):
        for key in ("data", "result", "items"):
            value = message.get(key)
            if isinstance(value, list):
                return value
    return []

def patient_name(record: dict) -> str:
    parts = [record.get("first_name"), record.get("last_name")]
    name = " ".join(str(part).strip() for part in parts if part)
    return name or str(record.get("full_name") or record.get("name") or "")

def cr_id_of(record: dict) -> str:
    for key in CR_ID_KEYS:
        value = record.get(key)
        if value:
            return str(value).strip()
    return ""

def parse_eligibility(payload) -> dict:
    data = payload
    if isinstance(payload, dict) and isinstance(payload.get("message"), dict):
        data = payload["message"]
    data = data if isinstance(data, dict) else {}
    schemes = []
    raw_schemes = data.get("schemes")
    if isinstance(raw_schemes, list):
        for item in raw_schemes:
            if not isinstance(item, dict):
                continue
            coverage = item.get("coverage") if isinstance(item.get("coverage"), dict) else {}
            coverage_status = str(coverage.get("status") or "").strip()
            coverage_message = str(coverage.get("message") or "").strip().lower()
            if coverage_status:
                covered = coverage_status == COVERAGE_ACTIVE_STATUS
            else:
                covered = "covered" in coverage_message and "not covered" not in coverage_message
            schemes.append(
                {
                    "scheme": str(item.get("schemeName") or "").strip(),
                    "scheme_id": item.get("schemeId"),
                    "member_type": str(item.get("memberType") or "").strip(),
                    "covered": covered,
                    "status": str(coverage.get("status") or "").strip(),
                    "message": str(coverage.get("message") or "").strip(),
                    "reason": str(coverage.get("reason") or "").strip(),
                }
            )
    status = str(data.get("statusDesc") or "").strip()
    if not status:
        for key in STATUS_KEYS:
            value = data.get(key)
            if value:
                status = str(value).strip()
                break
    scheme = ""
    if schemes:
        scheme = next((s["scheme"] for s in schemes if s["covered"]), schemes[0]["scheme"])
    if not scheme:
        for key in SCHEME_KEYS:
            value = data.get(key)
            if value:
                scheme = str(value).strip()
                break
    if schemes:
        eligible = any(s["covered"] for s in schemes)
    else:
        eligible = status.lower() in ELIGIBLE_STATUSES
    return {"eligible": eligible, "status": status, "scheme": scheme, "schemes": schemes}

def patient_profile(record: dict) -> dict:
    """Full register dossier for one patient/member record (primary or dependant)."""
    meta = record.get("meta") or {}
    origin = record.get("originSystem") or {}
    other_identifications = [
        {
            "identification_type": ident.get("identification_type") or "",
            "identification_number": ident.get("identification_number") or "",
        }
        for ident in record.get("other_identifications") or []
        if isinstance(ident, dict)
    ]
    return {
        "cr_id": cr_id_of(record),
        "name": patient_name(record),
        "title": record.get("title") or "",
        "first_name": record.get("first_name") or "",
        "middle_name": record.get("middle_name") or "",
        "last_name": record.get("last_name") or "",
        "gender": record.get("gender") or "",
        "date_of_birth": record.get("date_of_birth") or "",
        "place_of_birth": record.get("place_of_birth") or "",
        "citizenship": record.get("citizenship") or "",
        "civil_status": record.get("civil_status") or "",
        "employment_type": record.get("employment_type") or "",
        "employer_name": record.get("employer_name") or "",
        "msisdn": record.get("msisdn") or record.get("phone") or "",
        "email": record.get("email") or "",
        "client_status": record.get("client_status") or "",
        "identification_type": record.get("identification_type") or "",
        "identification_number": record.get("identification_number") or "",
        "other_identifications": other_identifications,
        "is_alive": bool(record.get("is_alive")),
        "deceased_datetime": record.get("deceased_datetime") or "",
        "identity_verified": bool(record.get("identity_verified")),
        "biometrics_verified": bool(record.get("biometrics_verified")),
        "person_with_disability": bool(record.get("person_with_disability")),
        "disability_certificate_no": record.get("disability_certificate_no") or "",
        "id_serial": record.get("id_serial") or "",
        "address": {
            "country": record.get("country") or "",
            "county": record.get("county") or "",
            "sub_county": record.get("sub_county") or "",
            "ward": record.get("ward") or "",
            "village_estate": record.get("village_estate") or "",
            "postal_address": record.get("postal_address") or "",
            "zip_code": record.get("zip_code") or "",
        },
        "contributions": {
            "estimated_contribution": record.get("estimated_contribution"),
            "estimated_annual_contribution": record.get("estimated_annual_contribution"),
            "gross_income": record.get("gross_income"),
            "gross_income_currency": record.get("gross_income_currency") or "",
        },
        "created": meta.get("creationTime") or "",
        "last_updated": meta.get("lastUpdated") or "",
        "source": meta.get("source") or "",
        "origin_system": origin.get("system") or "",
    }

def dependants_summary(patient: dict) -> list:
    dependants = []
    for entry in patient.get("dependants") or []:
        if not isinstance(entry, dict):
            continue
        members = entry.get("result")
        members = members if isinstance(members, list) else [entry]
        dependants.append({
            "relationship": entry.get("relationship") or "",
            "date_added": entry.get("date_added") or "",
            "total": entry.get("total") or len(members),
            "members": [patient_profile(member) for member in members if isinstance(member, dict)],
        })
    return dependants

def step_patient(filters: dict) -> dict:
    doc = settings_doc()
    origin = dha_origin()
    path = (doc.get("fetch_client") or "").strip()
    if not path:
        raise BadRequest("HISI Settings base_url / fetch_client are not configured.")
    identification_type = str(filters.get("identification_type") or "").strip()
    identification_number = str(filters.get("identification_number") or "").strip()
    if not identification_type or not identification_number:
        cr_number = str(filters.get("cr_id") or filters.get("client") or "").strip()
        if not cr_number:
            raise BadRequest("The patient step needs identification_type + identification_number filters, or a cr_id filter.")
        payload = {"id": cr_number}
    else:
        payload = {"identification_type": identification_type, "identification_number": identification_number}
    params = {"payload": json.dumps(payload)}
    patients = parse_records(http_get(join_url(origin, path), params))
    if not patients:
        raise NotFound("No patient found for the given identifier.")
    patient = patients[0]
    profile = patient_profile(patient)
    profile["identification_type"] = profile.get("identification_type") or identification_type
    profile["identification_number"] = profile.get("identification_number") or identification_number
    profile["dependants"] = dependants_summary(patient)
    return profile

def step_eligibility(cr_id: str, filters: dict) -> dict:
    doc = settings_doc()
    origin = dha_origin()
    path = (doc.get("eligibility_check") or "").strip()
    if not path:
        raise BadRequest("HISI Settings base_url / eligibility_check are not configured.")
    request_id_type = filters.get("requestIdType")
    request_id_number = filters.get("requestIdNumber")
    if request_id_type in (None, "") or request_id_number in (None, ""):
        if cr_id:
            # Prefer the CR ID resolved by the patient step (or cr_id filter);
            # DHA eligibility accepts it directly without an identification-type mapping.
            request_id_type = REQUEST_ID_TYPES["CR ID"]
            request_id_number = cr_id
        else:
            identification_type = str(filters.get("identification_type") or "").strip()
            identification_number = str(filters.get("identification_number") or "").strip()
            if identification_type and identification_number:
                request_id_type = REQUEST_ID_TYPES.get(identification_type)
                if request_id_type is None:
                    raise BadRequest("No requestIdType mapping for identification_type {}; pass requestIdType and requestIdNumber filters.".format(identification_type))
                request_id_number = identification_number
            else:
                raise BadRequest("The eligibility step needs requestIdType + requestIdNumber, a cr_id filter, or identification_type + identification_number.")
    body = {"requestIdType": int(request_id_type), "requestIdNumber": str(request_id_number)}
    return parse_eligibility(http_post(join_url(origin, path), body))

def step_consent(cr_id: str) -> dict:
    params = {
        "filters": json.dumps([["client", "=", cr_id]]),
        "fields": json.dumps(CONSENT_FIELDS),
    }
    doc = settings_doc()
    origin = dha_origin()
    # Simulation mode: the UAT CMP accepts the call unauthenticated;
    # production keeps the usual JWT bearer.
    bearer = not doc.get("enable_uat_simulation")
    records = [
        record
        for record in parse_records(http_get(join_url(origin, consent_resource_path()), params, bearer=bearer))
        if isinstance(record, dict) and str(record.get("visit_id") or "").strip()
    ]
    return {"records": records}

def step_claims(cr_id: str) -> dict:
    records = med_claim_records(med_claim_search_path(), cr_id)
    _attach_claim_activity(records, med_claim_search_path(), "name", "Claim Record")
    _attach_claim_visit_ids(records, med_claim_search_path())
    return {"records": records}

MAX_PREAUTH_PROCEDURE_LOOKUPS = 5

def step_preauth(cr_id: str) -> dict:
    records = med_claim_records(med_preauth_search_path(), cr_id)
    # Each preauth is enriched with its linked Procedure Records (the
    # procedure lookup is keyed by the preauth's claim id).
    path = med_preauth_procedure_search_path()
    origin = dha_origin()
    for index, record in enumerate(records):
        if index >= MAX_PREAUTH_PROCEDURE_LOOKUPS:
            record["procedures"] = []
            continue
        claim_id = str(record.get("name") or "").strip()
        if not claim_id:
            record["procedures"] = []
            continue
        params = {
            "filters": json.dumps([["claim_id", "=", claim_id]]),
            "fields": json.dumps(PROCEDURE_FIELDS),
        }
        try:
            record["procedures"] = [
                item
                for item in parse_records(http_get(join_url(origin, path), params, bearer=True))
                if isinstance(item, dict)
            ]
        except UpstreamError:
            record["procedures"] = []
    _attach_procedure_activity(records)
    _attach_claim_activity(records, med_preauth_search_path(), "bundle_id", "Shared Health Record")
    _attach_claim_visit_ids(records, med_preauth_search_path())
    return {"records": records}

def med_claim_records(path: str, cr_id: str) -> list:
    params = {
        "filters": json.dumps([["client", "=", cr_id]]),
        "fields": json.dumps(CLAIM_FIELDS),
    }
    origin = dha_origin()
    return [
        record
        for record in parse_records(http_get(join_url(origin, path), params, bearer=True))
        if isinstance(record, dict)
    ]

VERSION_FIELDS = ["docname", "creation", "data"]

# The Version diffs also carry the payer's echoed FHIR response payload
# (multi-KB per row) and geo / payer bookkeeping — only the status
# transition fields and their short human context reach the client.
ACTIVITY_CHANGE_FIELDS = {
    "claim_status",
    "validation_status",
    "practitioner_approval_status",
    "procedure_fulfil_status",
    "comment",
    "claim_upstream_errors",
    "end_procedure_reason",
}

def version_route_for(claim_route: str) -> str:
    """The registry's Version resource on the claim lane's route (the
    per-document change history): ``.../resource/Claim Record`` becomes
    ``.../resource/Version``."""
    return claim_route.rsplit("/", 1)[0] + "/Version"

def _version_changed(data) -> list:
    """A Version row's ``data`` is a change-log diff whose ``changed`` is a
    list of ``[field, old, new]`` triples; anything unparseable is a no-op
    (the row simply carries no readable changes)."""
    try:
        diff = json.loads(data) if isinstance(data, str) else data
    except (ValueError, TypeError):
        return []
    if not isinstance(diff, dict):
        return []
    changed = diff.get("changed")
    if not isinstance(changed, list):
        return []
    return [item for item in changed if isinstance(item, list) and item]

def claim_activity_by_doc(claim_route: str, docnames: list, ref_doctype: str) -> dict:
    """The registry's Version history for a batch of documents in ONE
    request (the search API accepts an ``in`` filter on ``docname``), as
    ``{docname: [{at, changes: [{field, from, to}]}]}`` in creation order.
    A fetch failure yields an empty map: the activity is an enrichment,
    and its loss degrades to the creation point alone rather than failing
    the step."""
    if not docnames:
        return {}
    params = {
        "filters": json.dumps([["ref_doctype", "=", ref_doctype], ["docname", "in", docnames]]),
        "fields": json.dumps(VERSION_FIELDS),
        "order_by": "creation asc",
    }
    try:
        payload = http_get(join_url(dha_origin(), version_route_for(claim_route)), params, bearer=True)
    except UpstreamError:
        return {}
    by_doc = {}
    for row in parse_records(payload):
        if not isinstance(row, dict):
            continue
        docname = str(row.get("docname") or "").strip()
        if not docname:
            continue
        changes = []
        for item in _version_changed(row.get("data")):
            field = str(item[0])
            if field not in ACTIVITY_CHANGE_FIELDS:
                continue
            old = item[1] if len(item) > 1 else None
            new = item[2] if len(item) > 2 else None
            changes.append({"field": field, "from": _text(old) or None, "to": _text(new) or None})
        if not changes:
            # The version touched only bookkeeping fields (the echoed FHIR
            # response, coordinates, payer references) — nothing to show.
            continue
        by_doc.setdefault(docname, []).append({"at": str(row.get("creation") or ""), "changes": changes})
    return by_doc

def _attach_claim_activity(records: list, claim_route: str, doc_field: str, ref_doctype: str) -> None:
    """Attach each record's Version history as ``activity`` — the record's
    real status transitions with their change timestamps. A record the
    registry has no versions for keeps an empty list, so the UI shows the
    creation point (sent) alone."""
    docnames = []
    for record in records:
        value = str(record.get(doc_field) or "").strip() if isinstance(record, dict) else ""
        if value and value not in docnames:
            docnames.append(value)
    by_doc = claim_activity_by_doc(claim_route, docnames, ref_doctype)
    for record in records:
        if not isinstance(record, dict):
            continue
        record["activity"] = by_doc.get(str(record.get(doc_field) or "").strip(), [])

def _attach_procedure_activity(records: list) -> None:
    """Attach each preauth's linked Procedure Records to their own Version
    history (their real fulfilment transitions, e.g. Upcoming -> Active ->
    Terminated) — one batched request covers every procedure of every
    preauth in the step."""
    docnames = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for procedure in record.get("procedures") or []:
            value = str(procedure.get("name") or "").strip() if isinstance(procedure, dict) else ""
            if value and value not in docnames:
                docnames.append(value)
    by_doc = claim_activity_by_doc(med_preauth_search_path(), docnames, "Procedure Record")
    for record in records:
        if not isinstance(record, dict):
            continue
        for procedure in record.get("procedures") or []:
            if not isinstance(procedure, dict):
                continue
            procedure["activity"] = by_doc.get(str(procedure.get("name") or "").strip(), [])

def visit_claim_context(claim_routes: list) -> tuple:
    """The claim<->visit join via the registry Shared Health Record.

    The claim/preauth registry has no visit field (2026-11-11 live probe:
    every candidate visit field name is rejected with 417 DataError), but
    every claim carries a ``bundle_id`` — the id of the Shared Health
    Record the claim was submitted in. That SHR resource (looked up on the
    claim's own registry route with ``name = bundle_id``) stores the
    submitted FHIR bundle in ``fhir_request``, whose Encounter carries the
    visit — the ``episodeOfCare`` reference, i.e. the consent ``visit_id``
    space (verified live on UAT: a claim's bundle resolves to the same
    EpisodeOfCare id the consent records carry).

    ``claim_routes`` is a list of ``(claim_record, claim_route)`` pairs,
    where ``claim_route`` is the registry path the claim came from (the
    configured ``med_claim_search`` / ``med_preauth_search`` value).
    Returns ``(claim_map, by_visit, problems)``: ``claim_map`` maps a claim
    id to its visit id ("" when the bundle has no Encounter to read from);
    ``by_visit`` maps a visit id to the submitted bundle's FHIR resources
    (the SHR side of the per-visit comparison, deduped across claims sharing
    a bundle); ``problems`` lists per-bundle failures so one bad bundle
    never sinks the rest of the join."""
    claim_map = {}
    by_visit = {}
    problems = []
    bundle_cache = {}
    for record, route in claim_routes or []:
        if not isinstance(record, dict):
            continue
        claim_id = str(record.get("name") or "").strip()
        if not claim_id:
            continue
        bundle_id = str(record.get("bundle_id") or "").strip()
        if not bundle_id:
            claim_map[claim_id] = ""
            continue
        if bundle_id not in bundle_cache:
            try:
                bundle_cache[bundle_id] = _fetch_shr_bundle(bundle_id, route)
            except (BadRequest, NotFound, UpstreamError) as exc:
                problems.append("bundle {}: {}".format(bundle_id, exc.message or exc))
                bundle_cache[bundle_id] = None
            except Exception as exc:
                problems.append("bundle {}: {}".format(bundle_id, exc))
                bundle_cache[bundle_id] = None
        bundle = bundle_cache[bundle_id]
        visit_id = visit_from_fhir_bundle(bundle) if bundle else ""
        claim_map[claim_id] = visit_id
        if visit_id and bundle:
            resources = [
                entry.get("resource")
                for entry in bundle.get("entry") or []
                if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)
            ]
            by_visit.setdefault(visit_id, resources)
    return claim_map, by_visit, problems

def _attach_claim_visit_ids(records: list, claim_route: str) -> None:
    """Attach each claim's own visit id — its submitted bundle's
    ``Encounter.episodeOfCare`` reference, the consent ``visit_id`` space
    — so the UI tags the claim/preauth with the same id the visit card
    shows (the records join on it). A claim whose bundle carries no
    readable encounter gets ``""`` (the UI falls back to the
    time-matched consent anchor)."""
    if not records:
        return
    claim_map, _, _ = visit_claim_context(
        [(record, claim_route) for record in records if isinstance(record, dict)]
    )
    for record in records:
        if not isinstance(record, dict):
            continue
        record["visit_id"] = str(claim_map.get(str(record.get("name") or "").strip(), "") or "")

def _fetch_shr_bundle(bundle_id: str, claim_route: str):
    """One Shared Health Record by id (the claim's ``bundle_id``) from the
    claim's own registry route — ``.../resource/Claim Record`` becomes
    ``.../resource/Shared Health Record``, so a preauth claim's bundle is
    read under the preauth route. Returns the submitted FHIR bundle parsed
    as a dict, or None when the resource carries no readable payload."""
    shr_path = claim_route.rsplit("/", 1)[0] + "/Shared Health Record"
    rows = parse_records(
        http_get(
            join_url(dha_origin(), shr_path),
            {"filters": json.dumps([["name", "=", bundle_id]]), "fields": json.dumps(["name", "fhir_request"])},
            bearer=True,
        )
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        payload = parse_shr_fhir_payload(row.get("fhir_request"))
        if isinstance(payload, dict):
            return payload
    return None

def parse_shr_fhir_payload(value):
    """The registry stores the FHIR payload as text: strict JSON in some
    rows, a Python dict repr (single quotes) in others — parse either and
    give up (None) on anything unparseable."""
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError, TypeError):
        return None

def visit_from_fhir_bundle(bundle) -> str:
    """The visit id a FHIR bundle belongs to: the first Encounter's
    ``episodeOfCare`` reference id (the consent ``visit_id`` space), falling
    back to the Encounter's ``.../identifier/visit`` identifier. Empty string
    when the bundle carries no Encounter (some claim bundles hold only
    Claim/Coverage/Patient/Organization — the visit is simply not
    recoverable from the claim side)."""
    for entry in (bundle or {}).get("entry") or []:
        resource = (entry or {}).get("resource") if isinstance(entry, dict) else None
        if not isinstance(resource, dict) or resource.get("resourceType") != "Encounter":
            continue
        for episode in resource.get("episodeOfCare") or []:
            ref = str((episode or {}).get("reference") or "").strip()
            if ref:
                return ref.rsplit("/", 1)[-1]
        for identifier in resource.get("identifier") or []:
            system = str((identifier or {}).get("system") or "")
            value = str((identifier or {}).get("value") or "").strip()
            if "visit" in system.lower() and value:
                return value
    return ""

def step_records(visit_id: str) -> dict:
    visit_id = str(visit_id or "").strip()
    if not visit_id:
        raise BadRequest("A visit_id is required for the records step: pass a visit_id filter or include the consent step.")
    return {"visit_id": visit_id, "records": fetch_patient_records(visit_id)}

def _visit_reference_id(reference: str) -> str:
    reference = reference.strip()
    return reference.rsplit("/", 1)[-1] if reference else ""

def _first_reference_id(value) -> str:
    """A FHIR reference field as upstream sends it: a single Reference
    object, or a 0..N array of them (episodeOfCare arrives as a one-item
    list). Returns the referenced resource's id, or '' when absent."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        return _visit_reference_id(_text(value.get("reference")))
    return ""

def _assign_patient_records_to_visits(by_visit: dict, by_resource: dict) -> dict:
    """Fold patient-level records into their visit buckets so the journey
    UI renders them: the per-visit fetch reveals only the visit's
    Encounter, while diagnosis and medication records reference the visit
    through that encounter and arrive via the patient-level fetch. A
    record's visit is resolved from its own episodeOfCare reference, or
    from the episodeOfCare of the Encounter it references (looked up
    across both the patient-level records and the existing buckets)."""
    patient_records = []
    pool = []
    for records in by_resource.values():
        if isinstance(records, list):
            for record in records:
                if isinstance(record, dict):
                    patient_records.append(record)
                    pool.append(record)
    for records in by_visit.values():
        if isinstance(records, list):
            pool.extend(record for record in records if isinstance(record, dict))
    encounters = {}
    for record in pool:
        if _text(record.get("resourceType")) == "Encounter" and _text(record.get("id")):
            encounters[_text(record.get("id"))] = record

    def visit_id_of(record: dict) -> str:
        visit = _first_reference_id(record.get("episodeOfCare"))
        if visit:
            return visit
        encounter = record.get("encounter")
        if isinstance(encounter, dict):
            linked = encounters.get(_visit_reference_id(_text(encounter.get("reference"))))
            if isinstance(linked, dict):
                return _first_reference_id(linked.get("episodeOfCare"))
        return ""

    for record in patient_records:
        visit = visit_id_of(record)
        if not visit:
            continue
        bucket = by_visit.setdefault(visit, [])
        key = _record_dedupe_key(record)
        if any(_record_dedupe_key(existing) == key for existing in bucket):
            continue
        bucket.append(record)
    return by_visit

def _attach_visit_facilities(by_visit: dict, org_cache: dict) -> None:
    """Resolve the facility Organization into every visit bucket, after the
    patient-level fold: the visit-scoped fetch only resolves the facilities
    of the visits it searched, and the encounters that arrive via the
    patient-level fetch still carry only the facility reference. A shared
    cache avoids re-fetching an Organization already resolved for another
    visit; buckets that already carry the Organization (patient-level fetch,
    earlier visit) are skipped."""
    pending = []
    for visit, records in by_visit.items():
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict) or _text(record.get("resourceType")) != "Encounter":
                continue
            for reference in _encounter_facility_refs(record):
                org_id = _visit_reference_id(reference)
                if not org_id:
                    continue
                present = any(
                    _record_dedupe_key(existing) == ("Organization", org_id) for existing in records if isinstance(existing, dict)
                )
                if not present:
                    pending.append((records, org_id))
    if not pending:
        return
    access_key, secret_key, region, data_store_id = aws_credentials()
    host = "healthlake.{}.amazonaws.com".format(region)
    seen = set()
    for records, org_id in pending:
        marker = (id(records), org_id)
        if marker in seen:
            continue
        seen.add(marker)
        if org_id not in org_cache:
            try:
                org_cache[org_id] = fetch_organization_by_id(org_id, host, access_key, secret_key, region, data_store_id)
            except UpstreamError:
                org_cache[org_id] = {}
        if org_cache[org_id]:
            # enrich on append (the visit-scoped path enriches its whole
            # list at once; the journey UI reads the curated headline).
            records.append(enrich_fhir_records([org_cache[org_id]])[0])

def step_records_multi(filters: dict, results: dict, cr_id: str = "") -> dict:
    # The visit_id filter accepts a comma-separated list so a records-only
    # call (the timeline lazy-loads consent first, then records with the
    # consented visits) can carry every consented visit in one request;
    # every listed visit is fetched — there is no visit cap.
    visit_ids = []
    for visit in str(filters.get("visit_id") or "").split(","):
        visit = visit.strip()
        if visit and visit not in visit_ids:
            visit_ids.append(visit)
    for record in (results.get(STEP_CONSENT) or {}).get("records") or []:
        candidate = str(record.get("visit_id") or "").strip()
        if candidate and candidate not in visit_ids:
            visit_ids.append(candidate)
    by_visit = {}
    errors = {}
    org_cache = {}
    for candidate in visit_ids:
        try:
            by_visit[candidate] = fetch_patient_records(candidate, org_cache)
        except UpstreamError as exc:
            by_visit[candidate] = []
            errors[candidate] = str(exc)
    configured_resources = [
        row for row in settings_doc().get("patient_resources") or [] if str(row.get("resource_type") or "").strip()
    ]
    by_resource = {}
    shr_id = ""
    if str(cr_id or "").strip() and configured_resources:
        try:
            shr_id = resolve_shr_patient_id(cr_id)
        except UpstreamError as exc:
            errors["shr"] = str(exc)
        by_resource, resource_errors = fetch_patient_level_records(shr_id, cr_id)
        errors.update(resource_errors)
    merged = []
    seen = set()
    for candidate in visit_ids:
        for record in by_visit.get(candidate) or []:
            if not isinstance(record, dict):
                continue
            key = _record_dedupe_key(record)
            if key in seen:
                continue
            seen.add(key)
            merged.append(record)
    by_resource_deduped = {}
    for resource_type, records in by_resource.items():
        fresh = []
        for record in records:
            if not isinstance(record, dict):
                continue
            key = _record_dedupe_key(record)
            if key in seen:
                continue
            seen.add(key)
            fresh.append(record)
            merged.append(record)
        by_resource_deduped[resource_type] = enrich_fhir_records(fresh)
    by_visit = _assign_patient_records_to_visits(by_visit, by_resource_deduped)
    _attach_visit_facilities(by_visit, org_cache)
    first = visit_ids[0] if visit_ids else ""
    return {
        "visit_id": first,
        "records": merged,
        "by_visit": by_visit,
        "by_resource": by_resource_deduped,
        "shr_id": shr_id,
        "errors": errors,
    }

def parse_request(data) -> tuple:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            raise BadRequest("data must be a JSON object.")
    if not isinstance(data, dict):
        raise BadRequest("data must be a JSON object.")
    steps = data.get("steps")
    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        raise BadRequest("filters must be an object.")
    if not isinstance(steps, list) or not steps:
        raise BadRequest("steps must be a non-empty list of: patient, eligibility, consent, records, contributions, claims, preauth.")
    cleaned = []
    for step in steps:
        step = str(step).strip().lower()
        if step not in KNOWN_STEPS:
            raise BadRequest("Unknown step {}; allowed steps: patient, eligibility, consent, records, contributions, claims, preauth.".format(step))
        if step not in cleaned:
            cleaned.append(step)
    return cleaned, filters

FS_TOKEN_CACHE_KEY = "fs_contribution_token"
MAX_CONTRIBUTION_PAGES = 10

def fs_settings() -> tuple:
    doc = settings_doc()
    base_url = (doc.get("fs_base_url") or "").strip()
    auth_path = (doc.get("fs_auth") or "").strip()
    ledger_path = (doc.get("fs_member_contribution") or "").strip()
    username = (doc.get("fs_username") or "").strip()
    password = doc.get_password("fs_password") or ""
    missing = [
        name
        for name, value in (
            ("fs_base_url", base_url),
            ("fs_auth", auth_path),
            ("fs_member_contribution", ledger_path),
            ("fs_username", username),
            ("fs_password", password),
        )
        if not value
    ]
    if missing:
        raise BadRequest("HISI Settings " + " / ".join(missing) + " are not configured.")
    return base_url, auth_path, ledger_path, username, password

def fs_http_get(url: str, params, headers: dict, not_found_ok: bool = False) -> dict:
    try:
        response = requests.get(url, params=params, headers=headers, timeout=upstream_timeout_seconds())
        response.raise_for_status()
    except requests.exceptions.Timeout:
        raise UpstreamError("FS request timed out.")
    except requests.exceptions.HTTPError as e:
        code = getattr(e.response, "status_code", None)
        if not_found_ok and code == 404:
            # A 404 from the ledger means the member has no contribution
            # transactions (or is unknown to the ledger) — an empty ledger is
            # the honest result, not a failure. Other HTTP errors still raise.
            return {"data": {"total": 0, "data": []}}
        raise UpstreamError("FS request failed with HTTP {}.".format(code if code is not None else "error"))
    except requests.exceptions.RequestException:
        raise UpstreamError("FS request failed.")
    try:
        return response.json()
    except ValueError:
        raise UpstreamError("FS returned a non-JSON response.")

def fs_token_expiry(token: str) -> float:
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(segment).decode("utf-8"))
        return float(payload["exp"]) - 60
    except (IndexError, ValueError, KeyError, AttributeError):
        return 0.0

def fs_login_token() -> str:
    cached = getattr(frappe.local, FS_TOKEN_CACHE_KEY, None)
    if cached and cached[1] > time.time():
        return cached[0]
    base_url, auth_path, _, username, password = fs_settings()
    try:
        response = requests.post(
            join_url(base_url, auth_path),
            json={"email": username, "password": password},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=upstream_timeout_seconds(),
        )
        response.raise_for_status()
    except requests.exceptions.Timeout:
        raise UpstreamError("FS login timed out.")
    except requests.exceptions.HTTPError as e:
        raise UpstreamError("FS login failed with HTTP {}.".format(getattr(e.response, "status_code", "error")))
    except requests.exceptions.RequestException:
        raise UpstreamError("FS login request failed.")
    try:
        body = response.json()
    except ValueError:
        raise UpstreamError("FS login returned a non-JSON response.")
    token = body.get("accessToken") if isinstance(body, dict) else None
    if not token:
        raise UpstreamError("FS login did not return an access token.")
    token = str(token)
    setattr(frappe.local, FS_TOKEN_CACHE_KEY, (token, fs_token_expiry(token)))
    return token

def fetch_patient_contributions(cr_id: str) -> dict:
    """Member contribution ledger for one CR ID: FS login + paginated ledger fetch."""
    cr_id = str(cr_id or "").strip()
    if not cr_id:
        raise BadRequest("A cr_id is required for the contributions step.")
    base_url, _, ledger_path, _, _ = fs_settings()
    headers = {"Authorization": "Bearer " + fs_login_token(), "Accept": "application/json"}
    url = join_url(join_url(base_url, ledger_path), cr_id)
    params = {"idType": str(REQUEST_ID_TYPES["CR ID"])}
    records = []
    total = None
    for _ in range(MAX_CONTRIBUTION_PAGES):
        payload = fs_http_get(url, params, headers, not_found_ok=True)
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}
        if total is None:
            total = data.get("total")
        page = data.get("data")
        if isinstance(page, list):
            records.extend(item for item in page if isinstance(item, dict))
        next_url = str(data.get("nextPageUrl") or "").strip()
        if not next_url:
            break
        url, params = next_url, None
    return {
        "cr_id": cr_id,
        "total": total,
        "contributions": [
            {
                "payment_reference": item.get("paymentReferenceNumber") or "",
                "invoice_number": item.get("invoiceNumber") or "",
                "employer": item.get("employerName") or "",
                "amount": item.get("amount"),
                "period": str(item.get("period") or ""),
            }
            for item in records
        ],
    }

@frappe.whitelist(methods=["POST", "GET"])
@api_endpoint
def patient_flow(data: str | dict = "") -> dict:
    steps, filters = parse_request(data)
    timeline_data_source()

    cr_id = ""
    if STEP_PATIENT in steps:
        result = step_patient(filters)
        cr_id = result["cr_id"]
        if not cr_id:
            raise NotFound("Patient found but no CR ID could be resolved.")
        results = {STEP_PATIENT: result}
    else:
        cr_id = str(filters.get("cr_id") or filters.get("client") or "").strip()
        if not cr_id and any(step in steps for step in (STEP_ELIGIBILITY, STEP_CONSENT, STEP_CONTRIBUTIONS, STEP_CLAIMS, STEP_PREAUTH)):
            raise BadRequest("A cr_id filter is required when the patient step is not included.")
        results = {}

    errors = {}

    def run_step(step, fn):
        # A failing step must not affect the subsequent ones: its error is
        # recorded per-step and the flow continues with the rest.
        try:
            results[step] = fn()
        except (UpstreamError, BadRequest) as exc:
            errors[step] = exc.message or str(exc)

    if STEP_ELIGIBILITY in steps:
        run_step(STEP_ELIGIBILITY, lambda: step_eligibility(cr_id, filters))
    if STEP_CONSENT in steps:
        run_step(STEP_CONSENT, lambda: step_consent(cr_id))
    if STEP_CONTRIBUTIONS in steps:
        run_step(STEP_CONTRIBUTIONS, lambda: fetch_patient_contributions(cr_id))
    if STEP_CLAIMS in steps:
        run_step(STEP_CLAIMS, lambda: step_claims(cr_id))
    if STEP_PREAUTH in steps:
        run_step(STEP_PREAUTH, lambda: step_preauth(cr_id))
    if STEP_RECORDS in steps:
        run_step(STEP_RECORDS, lambda: step_records_multi(filters, results, cr_id))

    payload = PatientFlowData(
        results=results,
        steps=steps,
        cr_id=cr_id,
        errors=errors,
    )
    return APIResponse.success(data=payload.model_dump(mode="json"))
