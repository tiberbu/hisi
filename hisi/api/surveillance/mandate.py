import hashlib
import hmac
import json
import secrets
import time

import frappe
from frappe.rate_limiter import rate_limit

from hisi.api.response_handler import APIResponse, BadRequest, Conflict, Forbidden, NotFound, api_endpoint
from hisi.api.patient import http_get
from hisi.api.surveillance.audit import log_event
from hisi.api.surveillance.errors import MandateInvalid
from hisi.api.surveillance.facilities import facility_search_url, parse_facility_list
from hisi.utils import active_companies, is_company_member

DATA_FMT = "%Y-%m-%d %H:%M:%S"
VERIFY_FAIL_MESSAGE = "This mandate code is not currently valid."


def settings_doc():
	return frappe.get_doc("HISI Settings")


def duration_ms(start):
	return int((time.time() - start) * 1000)


def caller_payer_companies(user):
	return active_companies(user)


def next_mandate_no():
	for _ in range(5):
		candidate = "SURV-{}".format(secrets.token_hex(3).upper())
		if not frappe.db.exists("HISI Surveillance Mandate", candidate):
			return candidate
	raise Conflict("Could not allocate a unique mandate number.")


def mandate_signature(payer_company, facility, assigned_officer, scope_lines, valid_from, expires_at, short_code):
	scope_hash = hashlib.sha256(json.dumps(scope_lines, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
	message = "\n".join([payer_company, facility, assigned_officer, scope_hash, str(valid_from), str(expires_at), short_code])
	key = "{}.surveillance-mandate".format(frappe.local.site).encode("utf-8")
	return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def parse_scope_lines(raw):
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			return None
	if not isinstance(raw, list) or not raw:
		return None
	cleaned = []
	for line in raw:
		if not isinstance(line, dict):
			return None
		line_type = str(line.get("line_type") or "").strip().lower()
		if line_type not in ("patient", "visit"):
			return None
		entry = {"line_type": line_type, "patient_identifier": None, "visit_id": None, "note": line.get("note")}
		if line_type == "patient":
			identifier = str(line.get("patient_identifier") or "").strip()
			if not identifier:
				return None
			entry["patient_identifier"] = identifier
		else:
			visit_id = str(line.get("visit_id") or "").strip()
			if not visit_id:
				return None
			entry["visit_id"] = visit_id
		cleaned.append(entry)
	return cleaned


def registry_facility(facility_name):
	"""Exact-match confirmation of the facility in the DHA facility registry.

	The registry is the sole source of facility identity and coordinates: a
	'HISI Health Facility' row is created only from a registry-confirmed row.
	Returns the registry row (dict) or None when there is no exact match.	"""
	payload = http_get(
		facility_search_url(),
		{
			"filters": json.dumps([["facility_name", "like", "%{}%".format(facility_name)]]),
			"fields": json.dumps(["*"]),
			"limit_page_length": 20,
			"limit_start": 0,
		},
	)
	target = facility_name.strip().lower()
	for row in parse_facility_list(payload):
		if str(row.get("facility_name") or "").strip().lower() == target:
			return row
	return None


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def issue_mandate(facility, assigned_officer, reason, scope_lines, valid_from, expires_at, radius_km=None, location_policy=None):
	start = time.time()
	user = frappe.session.user
	facility = str(facility or "").strip()

	def fail(message):
		log_event(None, user, "error", "mandate", facility, details={"reason": message}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest(message)

	if not facility:
		fail("A facility is required.")
	companies = caller_payer_companies(user)
	if not companies:
		log_event(None, user, "scope_denied", "mandate", facility, outcome="denied", duration_ms=duration_ms(start))
		raise Forbidden("You are not a member of a payer organization.")
	payer_company = companies[0]
	reason = str(reason or "").strip()
	if not reason or len(reason) > 500:
		fail("A reason is required (up to 500 characters).")
	assigned_officer = str(assigned_officer or "").strip()
	if not frappe.db.exists("User", assigned_officer):
		fail("The assigned officer is not a valid user.")
	scope = parse_scope_lines(scope_lines)
	if scope is None:
		fail("scope_lines must contain at least one valid patient or visit line.")
	try:
		window_from = frappe.utils.get_datetime(valid_from)
		window_until = frappe.utils.get_datetime(expires_at)
	except Exception:
		window_from = None
		window_until = None
	if not window_from or not window_until or window_until <= window_from:
		fail("valid_from and expires_at are required, with expires_at after valid_from.")
	policy = None
	if location_policy is not None:
		policy = str(location_policy).strip().lower()
		if policy not in ("strict", "flag"):
			fail("location_policy must be strict or flag.")
	radius = None
	if radius_km is not None:
		try:
			radius = float(radius_km)
		except (TypeError, ValueError):
			radius = None
		if radius is None or radius <= 0:
			fail("radius_km must be a positive number.")
	if not frappe.db.exists("HISI Health Facility", facility):
		try:
			row = registry_facility(facility)
		except Exception:
			fail("The facility registry could not be reached.")
		if row is None:
			fail("The facility was not found in the facility registry. Search the registry and pick the exact facility name.")
		latitude = row.get("latitude")
		longitude = row.get("longitude")
		try:
			latitude = float(latitude) if latitude is not None else None
			longitude = float(longitude) if longitude is not None else None
		except (TypeError, ValueError):
			latitude = None
			longitude = None
		if latitude is None or longitude is None:
			fail("The facility registry has no coordinates for this facility.")
		frappe.get_doc(
			{
				"doctype": "HISI Health Facility",
				"name": facility,
				"facility_code": str(row.get("facility_code") or "").strip() or None,
				"facility_fid": str(row.get("facility_fid") or "").strip() or None,
				"operational_status": str(row.get("operational_status") or "").strip() or None,
				"latitude": latitude,
				"longitude": longitude,
				"registered_by": payer_company,
			}
		).insert(ignore_permissions=True)
	short_code = secrets.token_hex(4).upper()
	mandate_no = next_mandate_no()
	valid_from_str = window_from.strftime(DATA_FMT)
	expires_str = window_until.strftime(DATA_FMT)
	qr_url = frappe.utils.get_url("/api/method/hisi.api.surveillance/verify_mandate?code=" + short_code)
	signature = mandate_signature(payer_company, facility, assigned_officer, scope, valid_from_str, expires_str, short_code)
	doc = frappe.get_doc(
		{
			"doctype": "HISI Surveillance Mandate",
			"mandate_no": mandate_no,
			"company": payer_company,
			"dispatched_by": user,
			"assigned_officer": assigned_officer,
			"facility": facility,
			"reason": reason,
			"valid_from": valid_from_str,
			"expires_at": expires_str,
			"short_code": short_code,
			"qr_url": qr_url,
			"radius_km": radius,
			"location_policy": policy,
			"signature": signature,
		}
	)
	for line in scope:
		doc.append(
			"scope_lines",
			{
				"doctype": "HISI Surveillance Scope Line",
				"line_type": line["line_type"],
				"patient_identifier": line["patient_identifier"],
				"visit_id": line["visit_id"],
				"note": line["note"],
			},
		)
	doc.insert(ignore_permissions=True)
	log_event(None, user, "mandate_issued", "mandate", mandate_no, {"company": payer_company, "facility": facility, "scope_lines": len(scope)}, "ok", duration_ms=duration_ms(start))
	return APIResponse.success(
		data={
			"mandate_no": mandate_no,
			"company": payer_company,
			"company_name": frappe.db.get_value("Company", payer_company, "company_name") or payer_company,
			"dispatched_by": user,
			"assigned_officer": assigned_officer,
			"facility": facility,
			"facility_name": facility,
			"reason": reason,
			"scope_lines": scope,
			"valid_from": valid_from_str,
			"expires_at": expires_str,
			"short_code": short_code,
			"qr_url": qr_url,
			"radius_km": radius,
			"location_policy": policy or ("strict" if settings_doc().get("surveillance_strict_location") else "flag"),
			"status": "dispatched",
			"signature": signature,
			"created_on": frappe.utils.now(),
		}
	)


@frappe.whitelist(methods=["GET", "POST"])
@rate_limit(limit=10, seconds=60)
@api_endpoint
def verify_mandate(code=None):
	start = time.time()
	user = frappe.session.user
	code = str(code or "").strip().upper()

	def deny(event_type="mandate_verify_failed"):
		log_event(None, user, event_type, "mandate", code or None, outcome="denied", duration_ms=duration_ms(start))
		raise MandateInvalid(VERIFY_FAIL_MESSAGE)

	if not code:
		log_event(None, user, "error", "mandate", None, details={"reason": "code missing"}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest("A mandate code is required.")
	try:
		threshold = int(settings_doc().get("surveillance_verify_lockout_attempts") or 0)
	except (TypeError, ValueError):
		threshold = 0
	if not threshold:
		log_event(None, user, "error", "mandate", code, details={"reason": "lockout threshold unset"}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest("HISI Settings surveillance_verify_lockout_attempts is not configured.")
	rows = frappe.get_all(
		"HISI Surveillance Mandate",
		filters={"short_code": code},
		fields=["name", "mandate_no", "status", "verify_locked", "verify_attempts", "valid_from", "expires_at", "assigned_officer", "facility"],
		limit=1,
	)
	if not rows:
		deny()
	mandate = rows[0]
	if mandate.status == "revoked":
		deny()
	if mandate.verify_locked:
		deny("mandate_verify_locked")
	now = frappe.utils.now_datetime()
	expires_at = frappe.utils.get_datetime(mandate.expires_at)
	if expires_at and now > expires_at:
		frappe.db.set_value("HISI Surveillance Mandate", mandate.name, "status", "expired")
		deny()
	valid_from = frappe.utils.get_datetime(mandate.valid_from)
	if valid_from and now < valid_from:
		deny()
	if mandate.assigned_officer != user:
		attempts = int(mandate.verify_attempts or 0) + 1
		if attempts >= threshold:
			frappe.db.set_value("HISI Surveillance Mandate", mandate.name, {"verify_attempts": attempts, "verify_locked": 1})
			deny("mandate_verify_locked")
		frappe.db.set_value("HISI Surveillance Mandate", mandate.name, "verify_attempts", attempts)
		deny()
	frappe.db.set_value("HISI Surveillance Mandate", mandate.name, "verify_attempts", 0)
	log_event(None, user, "mandate_verified", "mandate", mandate.mandate_no, {"facility": mandate.facility}, "ok", duration_ms=duration_ms(start))
	counts = {}
	for line in frappe.get_all("HISI Surveillance Scope Line", filters={"parent": mandate.name, "parenttype": "HISI Surveillance Mandate"}, fields=["line_type"]):
		counts[line["line_type"]] = counts.get(line["line_type"], 0) + 1
	return APIResponse.success(
		data={
			"valid": True,
			"mandate_no": mandate.mandate_no,
			"facility": {"name": mandate.facility},
			"scope_counts": counts,
			"valid_from": str(mandate.valid_from),
			"expires_at": str(mandate.expires_at),
			"assigned_to_me": True,
		}
	)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def revoke_mandate(mandate_no):
	start = time.time()
	user = frappe.session.user
	mandate_no = str(mandate_no or "").strip()
	if not mandate_no:
		log_event(None, user, "error", "mandate", None, details={"reason": "mandate_no missing"}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest("A mandate_no is required.")
	rows = frappe.get_all("HISI Surveillance Mandate", filters={"name": mandate_no}, fields=["name", "company", "status"], limit=1)
	if not rows:
		log_event(None, user, "error", "mandate", mandate_no, details={"reason": "not found"}, outcome="error", duration_ms=duration_ms(start))
		raise NotFound("Mandate not found.")
	mandate = rows[0]
	if not is_company_member(mandate.company, user):
		log_event(None, user, "scope_denied", "mandate", mandate_no, outcome="denied", duration_ms=duration_ms(start))
		raise Forbidden("You do not have access to this mandate.")
	if mandate.status in ("revoked", "expired", "completed"):
		log_event(None, user, "error", "mandate", mandate_no, details={"reason": "status is " + mandate.status}, outcome="error", duration_ms=duration_ms(start))
		raise Conflict("The mandate is already {}.".format(mandate.status))
	sessions = frappe.get_all("HISI Surveillance Session", filters={"mandate": mandate_no, "status": "active"}, fields=["name", "session_no", "user"])
	for row in sessions:
		frappe.db.set_value("HISI Surveillance Session", row["name"], "status", "revoked")
	frappe.db.set_value("HISI Surveillance Mandate", mandate_no, "status", "revoked")
	log_event(None, user, "mandate_revoked", "mandate", mandate_no, {"revoked_sessions": len(sessions)}, "ok", duration_ms=duration_ms(start))
	for row in sessions:
		log_event(row["name"], row["user"], "session_revoked", "session", row["session_no"], outcome="denied")
	return APIResponse.success(data={"mandate_no": mandate_no, "status": "revoked", "revoked_sessions": len(sessions)})
