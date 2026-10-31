import json
import secrets
import time
from datetime import timedelta

import frappe
from frappe.rate_limiter import rate_limit

from hisi.api.response_handler import APIResponse, BadRequest, Conflict, Forbidden, NotFound, Unauthorized, api_endpoint
from hisi.api.surveillance import location as geofence
from hisi.api.surveillance.audit import log_event, mask_ref, request_ip
from hisi.api.surveillance.errors import LocationCheckFailed, MandateInvalid, MandateRevoked

DATA_FMT = "%Y-%m-%d %H:%M:%S"
VERIFY_FAIL_MESSAGE = "This mandate code is not currently valid."
SESSION_FIELDS = [
	"name",
	"session_no",
	"mandate",
	"user",
	"status",
	"started_at",
	"ends_at",
	"last_activity",
	"location_status",
	"checkin_distance_m",
	"checkin_accuracy_m",
	"checkin_location",
	"device_meta",
	"checkin_flags",
	"summary",
	"surveiled_refs",
]
MANDATE_FIELDS = [
	"name",
	"assigned_officer",
	"mandate_no",
	"company",
	"status",
	"facility",
	"radius_km",
	"location_policy",
	"valid_from",
	"expires_at",
	"verify_locked",
]


def settings_doc():
	return frappe.get_doc("HISI Settings")


def duration_ms(start):
	return int((time.time() - start) * 1000)


def setting_int(field):
	value = settings_doc().get(field)
	try:
		number = int(value)
	except (TypeError, ValueError):
		raise BadRequest("HISI Settings {} is not configured.".format(field))
	if number <= 0:
		raise BadRequest("HISI Settings {} must be a positive number.".format(field))
	return number


def parse_payload(raw):
	if raw in (None, ""):
		return None
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			return None
	return raw if isinstance(raw, dict) else None


def parse_checkin(raw):
	if not raw:
		return None
	try:
		lat = float(raw.get("lat"))
		lng = float(raw.get("lng"))
	except (TypeError, ValueError):
		return None
	fix = {
		"lat": lat,
		"lng": lng,
		"accuracy_m": raw.get("accuracy_m"),
		"ts": raw.get("ts"),
		"platform": raw.get("platform") or "web",
	}
	if raw.get("is_mock") in (True, 1, "true", "True"):
		fix["is_mock"] = True
	return fix


def build_device_meta(fix, device=None):
	device = parse_payload(device) or {}
	return {
		"platform": (fix or {}).get("platform") or device.get("platform") or "web",
		"app_version": device.get("app_version"),
		"user_agent": device.get("user_agent"),
		"ip": request_ip(),
	}


def resolve_session_row(session_id):
	session_id = str(session_id or "").strip()
	if not session_id:
		return None
	rows = frappe.get_all("HISI Surveillance Session", filters={"session_no": session_id}, fields=SESSION_FIELDS, limit=1)
	return rows[0] if rows else None


def mandate_row(mandate_name):
	rows = frappe.get_all("HISI Surveillance Mandate", filters={"name": mandate_name}, fields=MANDATE_FIELDS, limit=1)
	return rows[0] if rows else None


def facility_row(facility_name):
	rows = frappe.get_all(
		"HISI Health Facility",
		filters={"name": facility_name},
		fields=["name", "latitude", "longitude", "surveillance_radius_km"],
		limit=1,
	)
	return rows[0] if rows else None


def scope_lines(mandate_name):
	return frappe.get_all(
		"HISI Surveillance Scope Line",
		filters={"parent": mandate_name, "parenttype": "HISI Surveillance Mandate"},
		fields=["line_type", "patient_identifier", "visit_id", "note"],
	)


def masked_scope_lines(mandate_name):
	lines = []
	for line in scope_lines(mandate_name):
		entry = {"line_type": line["line_type"]}
		if line["line_type"] == "patient":
			entry["patient_ref_masked"] = mask_ref(line["patient_identifier"])
		else:
			entry["visit_ref_masked"] = mask_ref(line["visit_id"])
		if line["note"]:
			entry["note"] = line["note"]
		lines.append(entry)
	return lines


def expire_session(row, reason):
	frappe.db.set_value("HISI Surveillance Session", row["name"], "status", "expired")
	log_event(row["name"], row["user"], "session_expired", "session", row["session_no"], details={"reason": reason}, outcome="denied")
	raise Forbidden("The session has expired.")


def require_session(session_id, touch=True):
	user = frappe.session.user
	session_id = str(session_id or "").strip()
	if not session_id:
		log_event(None, user, "error", "session", None, details={"reason": "session_id missing"}, outcome="error")
		raise BadRequest("A session_id is required.")
	row = resolve_session_row(session_id)
	if row is None:
		log_event(None, user, "error", "session", session_id, details={"reason": "session not found"}, outcome="error")
		raise NotFound("Session not found.")
	mandate = mandate_row(row["mandate"])
	if mandate is None:
		log_event(row["name"], user, "error", "session", row["session_no"], details={"reason": "mandate missing"}, outcome="error")
		raise NotFound("The session mandate is missing.")
	if mandate.status == "revoked" or row["status"] == "revoked":
		log_event(row["name"], user, "auth_denied", "session", row["session_no"], outcome="denied")
		raise MandateRevoked("The mandate has been revoked.")
	if row["user"] != user:
		log_event(row["name"], user, "auth_denied", "session", row["session_no"], outcome="denied")
		raise Forbidden("You do not have access to this session.")
	if row["status"] == "expired":
		log_event(row["name"], user, "session_expired", "session", row["session_no"], details={"reason": "already expired"}, outcome="denied")
		raise Forbidden("The session has expired.")
	if row["status"] == "completed":
		log_event(row["name"], user, "auth_denied", "session", row["session_no"], details={"reason": "session completed"}, outcome="denied")
		raise Forbidden("The session has ended.")
	now = frappe.utils.now_datetime()
	ends_at = frappe.utils.get_datetime(row["ends_at"])
	if ends_at and now > ends_at:
		expire_session(row, "ttl")
	last_activity = frappe.utils.get_datetime(row["last_activity"])
	idle_minutes = setting_int("surveillance_idle_timeout_minutes")
	if last_activity and now > last_activity + timedelta(minutes=idle_minutes):
		expire_session(row, "idle")
	if touch:
		frappe.db.set_value("HISI Surveillance Session", row["name"], "last_activity", now.strftime(DATA_FMT))
	return {"session": row, "mandate": mandate}


def append_flags(session_name, new_flags):
	current = frappe.db.get_value("HISI Surveillance Session", session_name, "checkin_flags")
	flags = json.loads(current or "[]")
	for flag in new_flags:
		if flag not in flags:
			flags.append(flag)
	frappe.db.set_value("HISI Surveillance Session", session_name, "checkin_flags", json.dumps(flags))
	return flags


def update_checkin_fields(row, result, fix, device=None):
	accuracy = fix.get("accuracy_m")
	try:
		accuracy = float(accuracy) if accuracy is not None else None
	except (TypeError, ValueError):
		accuracy = None
	frappe.db.set_value(
		"HISI Surveillance Session",
		row["name"],
		{
			"location_status": result.status,
			"checkin_distance_m": round(result.distance_m, 1) if result.distance_m is not None else None,
			"checkin_accuracy_m": accuracy,
			"checkin_location": json.dumps(fix) if fix else None,
			"device_meta": json.dumps(build_device_meta(fix, device)),
		},
	)
	return accuracy


def apply_checkin(row, result, fix, device=None):
	new_flags = []
	if result.status != "verified":
		new_flags.append(result.status)
	for flag in result.flags:
		if flag not in new_flags:
			new_flags.append(flag)
	accuracy = update_checkin_fields(row, result, fix, device)
	append_flags(row["name"], new_flags)
	return accuracy


def mandate_for_start(code, user, start):
	rows = frappe.get_all("HISI Surveillance Mandate", filters={"short_code": code}, fields=MANDATE_FIELDS, limit=1)
	if not rows:
		log_event(None, user, "mandate_verify_failed", "mandate", code, outcome="denied", duration_ms=duration_ms(start))
		raise MandateInvalid(VERIFY_FAIL_MESSAGE)
	mandate = rows[0]
	now = frappe.utils.now_datetime()
	if mandate.status == "revoked":
		log_event(None, user, "mandate_verify_failed", "mandate", code, outcome="denied", duration_ms=duration_ms(start))
		raise MandateInvalid(VERIFY_FAIL_MESSAGE)
	if mandate.verify_locked:
		log_event(None, user, "mandate_verify_locked", "mandate", code, outcome="denied", duration_ms=duration_ms(start))
		raise MandateInvalid(VERIFY_FAIL_MESSAGE)
	expires_at = frappe.utils.get_datetime(mandate.expires_at)
	if expires_at and now > expires_at:
		frappe.db.set_value("HISI Surveillance Mandate", mandate.name, "status", "expired")
		log_event(None, user, "mandate_verify_failed", "mandate", code, outcome="denied", duration_ms=duration_ms(start))
		raise MandateInvalid(VERIFY_FAIL_MESSAGE)
	valid_from = frappe.utils.get_datetime(mandate.valid_from)
	if valid_from and now < valid_from:
		log_event(None, user, "mandate_verify_failed", "mandate", code, outcome="denied", duration_ms=duration_ms(start))
		raise MandateInvalid(VERIFY_FAIL_MESSAGE)
	if mandate.assigned_officer != user:
		log_event(None, user, "auth_denied", "mandate", mandate.mandate_no, outcome="denied", duration_ms=duration_ms(start))
		raise Unauthorized("This mandate is not assigned to you.")
	return mandate


def next_session_no():
	for _ in range(5):
		candidate = "SS-{}".format(secrets.token_hex(3).upper())
		if not frappe.db.exists("HISI Surveillance Session", candidate):
			return candidate
	raise Conflict("Could not allocate a unique session number.")


def checkin_failure_message(result):
	distance = "unknown" if result.distance_m is None else "{:.0f}".format(result.distance_m)
	if result.status == "unreported":
		return "Check-in failed: no usable location was reported."
	if result.status == "spoofed":
		return "Check-in failed: the reported location is a mock ({} m from the facility).".format(distance)
	if "stale_timestamp" in result.flags:
		return "Check-in failed: the reported timestamp is stale ({} m from the facility).".format(distance)
	if "low_accuracy" in result.flags:
		return "Check-in failed: the reported GPS accuracy is below the required threshold ({} m from the facility).".format(distance)
	return "Check-in failed: the reported location is outside the geofence ({} m from the facility).".format(distance)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def start_session(mandate_code, location=None, device=None):
	start = time.time()
	user = frappe.session.user
	code = str(mandate_code or "").strip().upper()
	if not code:
		log_event(None, user, "error", "mandate", None, details={"reason": "mandate_code missing"}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest("A mandate_code is required.")
	mandate = mandate_for_start(code, user, start)
	facility = facility_row(mandate.facility)
	if facility is None or facility["latitude"] is None or facility["longitude"] is None:
		log_event(None, user, "error", "mandate", mandate.mandate_no, details={"reason": "facility has no stored coordinates"}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest("The facility has no stored coordinates, so the geofence cannot be evaluated.")
	radius_km = geofence.resolve_radius_km(mandate.radius_km, facility["surveillance_radius_km"], settings_doc().get("surveillance_default_radius_km"))
	max_accuracy_m = setting_int("surveillance_max_accuracy_m")
	fix = parse_checkin(parse_payload(location))
	result = geofence.evaluate_checkin(fix, facility["latitude"], facility["longitude"], radius_km, max_accuracy_m)
	settings = settings_doc()
	policy = "flag" if not settings.get("surveillance_strict_location") else (mandate.location_policy or "strict")
	if result.status != "verified" and policy == "strict":
		log_event(None, user, "checkin_failed", "facility", facility["name"], details={"status": result.status, "distance_m": result.distance_m, "flags": result.flags}, outcome="denied", location=fix, duration_ms=duration_ms(start))
		raise LocationCheckFailed(checkin_failure_message(result))
	now = frappe.utils.now_datetime()
	ttl_minutes = setting_int("surveillance_session_ttl_minutes")
	session_no = next_session_no()
	flags = list(result.flags) if result.status != "verified" else []
	accuracy = fix.get("accuracy_m") if fix else None
	try:
		accuracy = float(accuracy) if accuracy is not None else None
	except (TypeError, ValueError):
		accuracy = None
	ends_at = now + timedelta(minutes=ttl_minutes)
	frappe.get_doc(
		{
			"doctype": "HISI Surveillance Session",
			"session_no": session_no,
			"mandate": mandate.name,
			"company": mandate.company,
			"user": user,
			"status": "active",
			"started_at": now.strftime(DATA_FMT),
			"ends_at": ends_at.strftime(DATA_FMT),
			"last_activity": now.strftime(DATA_FMT),
			"location_status": result.status,
			"checkin_distance_m": round(result.distance_m, 1) if result.distance_m is not None else None,
			"checkin_accuracy_m": accuracy,
			"checkin_location": json.dumps(fix) if fix else None,
			"device_meta": json.dumps(build_device_meta(fix, device)),
			"checkin_flags": json.dumps(flags),
			"surveiled_refs": "[]",
		}
	).insert(ignore_permissions=True)
	if mandate.status == "dispatched":
		frappe.db.set_value("HISI Surveillance Mandate", mandate.name, "status", "active")
	checkin_event = "checkin_verified" if result.status == "verified" else "checkin_failed"
	log_event(None, user, checkin_event, "facility", facility["name"], details={"status": result.status, "distance_m": result.distance_m, "flags": result.flags, "policy": policy}, outcome="ok" if result.status == "verified" else "flagged", location=fix, duration_ms=duration_ms(start))
	log_event(session_no, user, "session_started", "session", session_no, details={"mandate": mandate.mandate_no, "policy": policy}, outcome="ok", duration_ms=duration_ms(start))
	return APIResponse.success(
		data={
			"session_no": session_no,
			"status": "active",
			"location_status": result.status,
			"checkin_distance_m": round(result.distance_m, 1) if result.distance_m is not None else None,
			"checkin_accuracy_m": accuracy,
			"flags": flags,
			"started_at": now.strftime(DATA_FMT),
			"ends_at": ends_at.strftime(DATA_FMT),
			"time_remaining_sec": ttl_minutes * 60,
			"mandate_no": mandate.mandate_no,
			"facility": facility["name"],
		}
	)


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=300, seconds=60)
@api_endpoint
def get_session(session_id):
	ctx = require_session(session_id, touch=False)
	row = ctx["session"]
	mandate = ctx["mandate"]
	now = frappe.utils.now_datetime()
	ends_at = frappe.utils.get_datetime(row["ends_at"])
	time_remaining = int((ends_at - now).total_seconds()) if ends_at else 0
	return APIResponse.success(
		data={
			"session": {
				"session_no": row["session_no"],
				"status": row["status"],
				"mandate_no": mandate.mandate_no,
				"started_at": str(row["started_at"]),
				"ends_at": str(row["ends_at"]),
				"location_status": row["location_status"],
				"checkin_distance_m": row["checkin_distance_m"],
				"checkin_accuracy_m": row["checkin_accuracy_m"],
				"flags": json.loads(row["checkin_flags"] or "[]"),
				"surveiled_refs": json.loads(row["surveiled_refs"] or "[]"),
			},
			"mandate": {
				"mandate_no": mandate.mandate_no,
				"facility": mandate.facility,
				"company": mandate.company,
				"valid_from": str(mandate.valid_from),
				"expires_at": str(mandate.expires_at),
			},
			"scope_lines": masked_scope_lines(mandate.name),
			"time_remaining_sec": max(0, time_remaining),
		}
	)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def end_session(session_id, summary=None, location=None, device=None):
	start = time.time()
	user = frappe.session.user
	session_id = str(session_id or "").strip()
	if not session_id:
		log_event(None, user, "error", "session", None, details={"reason": "session_id missing"}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest("A session_id is required.")
	row = resolve_session_row(session_id)
	if row is None:
		log_event(None, user, "error", "session", session_id, details={"reason": "session not found"}, outcome="error", duration_ms=duration_ms(start))
		raise NotFound("Session not found.")
	if row["status"] == "completed":
		log_event(row["name"], user, "error", "session", row["session_no"], details={"reason": "already completed"}, outcome="error", duration_ms=duration_ms(start))
		raise Conflict("The session has already ended.")
	ctx = require_session(session_id, touch=True)
	row = ctx["session"]
	fix = parse_checkin(parse_payload(location))
	if fix is not None:
		mandate = ctx["mandate"]
		facility = facility_row(mandate.facility)
		if facility is not None and facility["latitude"] is not None and facility["longitude"] is not None:
			radius_km = geofence.resolve_radius_km(mandate.radius_km, facility["surveillance_radius_km"], settings_doc().get("surveillance_default_radius_km"))
			result = geofence.evaluate_checkin(fix, facility["latitude"], facility["longitude"], radius_km, setting_int("surveillance_max_accuracy_m"))
			apply_checkin(row, result, fix, device)
	now = frappe.utils.now_datetime()
	frappe.db.set_value(
		"HISI Surveillance Session",
		row["name"],
		{"summary": str(summary or ""), "ended_at": now.strftime(DATA_FMT), "status": "completed"},
	)
	log_event(row["name"], user, "session_ended", "session", row["session_no"], details={"summary": str(summary or None)}, outcome="ok", duration_ms=duration_ms(start))
	return APIResponse.success(data={"session_no": row["session_no"], "status": "completed", "ended_at": now.strftime(DATA_FMT)})


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def recheck_location(session_id, location=None, device=None):
	start = time.time()
	user = frappe.session.user
	ctx = require_session(session_id, touch=True)
	row = ctx["session"]
	mandate = ctx["mandate"]
	if not settings_doc().get("surveillance_recheck_enabled"):
		log_event(row["name"], user, "error", "session", row["session_no"], details={"reason": "recheck disabled in settings"}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest("Mid-session re-check-in is disabled.")
	facility = facility_row(mandate.facility)
	if facility is None or facility["latitude"] is None or facility["longitude"] is None:
		log_event(row["name"], user, "error", "session", row["session_no"], details={"reason": "facility has no stored coordinates"}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest("The facility has no stored coordinates, so the geofence cannot be evaluated.")
	radius_km = geofence.resolve_radius_km(mandate.radius_km, facility["surveillance_radius_km"], settings_doc().get("surveillance_default_radius_km"))
	fix = parse_checkin(parse_payload(location))
	result = geofence.evaluate_checkin(fix, facility["latitude"], facility["longitude"], radius_km, setting_int("surveillance_max_accuracy_m"))
	new_flags = []
	accuracy = None
	if fix is not None:
		if result.status != "verified":
			new_flags.append(result.status)
		for flag in result.flags:
			if flag not in new_flags:
				new_flags.append(flag)
		previous = json.loads(row["checkin_location"] or "null")
		if previous and geofence.impossible_move(previous, fix):
			new_flags.append("impossible_move")
		accuracy = update_checkin_fields(row, result, fix, device)
	else:
		new_flags = ["unreported"]
	append_flags(row["name"], new_flags)
	outcome = "ok" if result.status == "verified" and not new_flags else "flagged"
	log_event(row["name"], user, "session_recheck", "facility", facility["name"], details={"status": result.status, "distance_m": result.distance_m, "flagged": new_flags}, outcome=outcome, location=fix, duration_ms=duration_ms(start))
	return APIResponse.success(
		data={
			"session_no": row["session_no"],
			"location_status": result.status,
			"checkin_distance_m": round(result.distance_m, 1) if result.distance_m is not None else None,
			"checkin_accuracy_m": accuracy,
			"flagged": new_flags,
		}
	)


@frappe.whitelist()
def backfill_expired_sessions():
	now = frappe.utils.now_datetime()
	settings = settings_doc()
	idle_raw = settings.get("surveillance_idle_timeout_minutes")
	try:
		idle_minutes = int(idle_raw) if idle_raw is not None else 0
	except (TypeError, ValueError):
		idle_minutes = 0
	rows = frappe.get_all(
		"HISI Surveillance Session",
		filters={"status": "active"},
		fields=["name", "session_no", "user", "ends_at", "last_activity"],
	)
	expired = 0
	for row in rows:
		ends_at = frappe.utils.get_datetime(row["ends_at"]) if row["ends_at"] else None
		last_activity = frappe.utils.get_datetime(row["last_activity"]) if row["last_activity"] else None
		reason = None
		if ends_at and now > ends_at:
			reason = "ttl"
		elif idle_minutes and last_activity and now > last_activity + timedelta(minutes=idle_minutes):
			reason = "idle"
		if reason:
			frappe.db.set_value("HISI Surveillance Session", row["name"], "status", "expired")
			log_event(row["name"], row["user"], "session_expired", "session", row["session_no"], details={"reason": reason}, outcome="denied")
			expired += 1
	return expired
