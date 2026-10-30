import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
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
]
STEP_PATIENT = "patient"
STEP_ELIGIBILITY = "eligibility"
STEP_CONSENT = "consent"
STEP_RECORDS = "records"
KNOWN_STEPS = (STEP_PATIENT, STEP_ELIGIBILITY, STEP_CONSENT, STEP_RECORDS)
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
    simulation: bool
    base_url: str
    fetched_at: str

def settings_doc():
    return frappe.get_doc("HISI Settings")

def join_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + ("/" + path.lstrip("/") if path else "")

def simulation_base_url() -> str:
    value = (settings_doc().get("uat_base_url") or "").strip()
    if not value:
        raise BadRequest("HISI Settings uat_base_url is not configured.")
    return value

def upstream_timeout_seconds() -> int:
    value = settings_doc().get("upstream_api_timeout")
    return int(value) if value else DEFAULT_TIMEOUT_SECONDS

def jwt_ttl_seconds() -> int:
    value = settings_doc().get("jwt_ttl_s")
    return int(value) if value else DEFAULT_JWT_TTL_SECONDS

def consent_resource_path() -> str:
    value = (settings_doc().get("fetch_patient_consent") or "").strip()
    if not value:
        raise BadRequest("HISI Settings fetch_patient_consent is not configured.")
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

def request_headers(bearer: bool) -> dict:
    return {"Authorization": "Bearer " + dha_jwt()} if bearer else {}

def http_get(url: str, params: dict, bearer: bool = True) -> dict:
    try:
        response = requests.get(url, params=params, headers=request_headers(bearer), timeout=upstream_timeout_seconds())
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

def http_post(url: str, body: dict, bearer: bool = True) -> dict:
    try:
        response = requests.post(url, json=body, headers=request_headers(bearer), timeout=upstream_timeout_seconds())
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
        EMPTY_PAYLOAD_SHA256,
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

def fetch_patient_records(visit_id: str) -> list:
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
    return parse_fhir_bundle(aws_get(path, params, host, access_key, secret_key, region))

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

def step_patient(filters: dict) -> dict:
    doc = settings_doc()
    base_url = (doc.get("base_url") or "").strip()
    path = (doc.get("fetch_client") or "").strip()
    if not base_url or not path:
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
    patients = parse_records(http_get(join_url(base_url, path), params))
    if not patients:
        raise NotFound("No patient found for the given identifier.")
    patient = patients[0]
    return {
        "cr_id": cr_id_of(patient),
        "name": patient_name(patient),
        "gender": patient.get("gender") or "",
        "msisdn": patient.get("msisdn") or "",
        "date_of_birth": patient.get("date_of_birth") or "",
        "client_status": patient.get("client_status") or "",
        "identification_type": patient.get("identification_type") or identification_type,
        "identification_number": patient.get("identification_number") or identification_number,
    }

def step_eligibility(cr_id: str, filters: dict) -> dict:
    doc = settings_doc()
    base_url = (doc.get("base_url") or "").strip()
    path = (doc.get("eligibility_check") or "").strip()
    if not base_url or not path:
        raise BadRequest("HISI Settings base_url / eligibility_check are not configured.")
    request_id_type = filters.get("requestIdType")
    request_id_number = filters.get("requestIdNumber")
    if request_id_type in (None, "") or request_id_number in (None, ""):
        identification_type = str(filters.get("identification_type") or "").strip()
        identification_number = str(filters.get("identification_number") or "").strip()
        if identification_type and identification_number:
            request_id_type = REQUEST_ID_TYPES.get(identification_type)
            if request_id_type is None:
                raise BadRequest("No requestIdType mapping for identification_type {}; pass requestIdType and requestIdNumber filters.".format(identification_type))
            request_id_number = identification_number
        elif cr_id:
            request_id_type = REQUEST_ID_TYPES["CR ID"]
            request_id_number = cr_id
        else:
            raise BadRequest("The eligibility step needs requestIdType + requestIdNumber, identification_type + identification_number, or a cr_id filter.")
    body = {"requestIdType": int(request_id_type), "requestIdNumber": str(request_id_number)}
    return parse_eligibility(http_post(join_url(base_url, path), body))

def step_consent(cr_id: str) -> dict:
    params = {
        "filters": json.dumps([["client", "=", cr_id]]),
        "fields": json.dumps(CONSENT_FIELDS),
    }
    records = [
        record
        for record in parse_records(http_get(join_url(simulation_base_url(), consent_resource_path()), params, bearer=False))
        if isinstance(record, dict) and str(record.get("visit_id") or "").strip()
    ]
    return {"records": records, "base_url": simulation_base_url()}

def step_records(visit_id: str) -> dict:
    visit_id = str(visit_id or "").strip()
    if not visit_id:
        raise BadRequest("A visit_id is required for the records step: pass a visit_id filter or include the consent step.")
    return {"visit_id": visit_id, "records": fetch_patient_records(visit_id)}

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
        raise BadRequest("steps must be a non-empty list of: patient, eligibility, consent, records.")
    cleaned = []
    for step in steps:
        step = str(step).strip().lower()
        if step not in KNOWN_STEPS:
            raise BadRequest("Unknown step {}; allowed steps: patient, eligibility, consent.".format(step))
        if step not in cleaned:
            cleaned.append(step)
    return cleaned, filters

@frappe.whitelist(methods=["POST", "GET"])
@api_endpoint
def patient_flow(data: str | dict = "") -> dict:
    steps, filters = parse_request(data)
    doc = settings_doc()
    base_url = (doc.get("base_url") or "").strip()
    simulation = bool(doc.get("enable_uat_simulation"))

    cr_id = ""
    if STEP_PATIENT in steps:
        result = step_patient(filters)
        cr_id = result["cr_id"]
        if not cr_id:
            raise NotFound("Patient found but no CR ID could be resolved.")
        results = {STEP_PATIENT: result}
    else:
        cr_id = str(filters.get("cr_id") or filters.get("client") or "").strip()
        if not cr_id and any(step in steps for step in (STEP_ELIGIBILITY, STEP_CONSENT)):
            raise BadRequest("A cr_id filter is required when the patient step is not included.")
        results = {}

    if STEP_ELIGIBILITY in steps:
        results[STEP_ELIGIBILITY] = step_eligibility(cr_id, filters)
    if STEP_CONSENT in steps:
        results[STEP_CONSENT] = step_consent(cr_id)
    if STEP_RECORDS in steps:
        visit_id = str(filters.get("visit_id") or "").strip()
        if not visit_id:
            for record in (results.get(STEP_CONSENT) or {}).get("records") or []:
                visit_id = str(record.get("visit_id") or "").strip()
                if visit_id:
                    break
        results[STEP_RECORDS] = step_records(visit_id)

    payload = PatientFlowData(
        results=results,
        steps=steps,
        cr_id=cr_id,
        simulation=simulation,
        base_url=base_url,
        fetched_at=frappe.utils.now(),
    )
    return APIResponse.success(data=payload.model_dump(mode="json"))
