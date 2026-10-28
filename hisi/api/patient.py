import base64
import hashlib
import hmac
import json
import time
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
KNOWN_STEPS = (STEP_PATIENT, STEP_ELIGIBILITY, STEP_CONSENT)
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
    parts = [record.get("first_name"), record.get("middle_name"), record.get("last_name")]
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
    return {"records": parse_records(http_get(join_url(simulation_base_url(), consent_resource_path()), params, bearer=False)), "base_url": simulation_base_url()}

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
        raise BadRequest("steps must be a non-empty list of: patient, eligibility, consent.")
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
        if not cr_id:
            raise BadRequest("A cr_id filter is required when the patient step is not included.")
        results = {}

    if STEP_ELIGIBILITY in steps:
        results[STEP_ELIGIBILITY] = step_eligibility(cr_id, filters)
    if STEP_CONSENT in steps:
        results[STEP_CONSENT] = step_consent(cr_id)

    payload = PatientFlowData(
        results=results,
        steps=steps,
        cr_id=cr_id,
        simulation=simulation,
        base_url=base_url,
        fetched_at=frappe.utils.now(),
    )
    return APIResponse.success(data=payload.model_dump(mode="json"))
