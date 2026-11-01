import base64
import json
import os
import re
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
	# The idle clock is suspended while paused (the officer may step away);
	# the TTL bound above still applies, so a paused session cannot outlive
	# its window.
	if row["status"] != "paused":
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


def geofence_for_mandate(mandate, location):
	"""Resolve the facility geofence and evaluate a client fix against it.
	Shared by the session-opening path and the paused-session resume path."""
	facility = facility_row(mandate.facility)
	if facility is None or facility["latitude"] is None or facility["longitude"] is None:
		raise BadRequest("The facility has no stored coordinates, so the geofence cannot be evaluated.")
	radius_km = geofence.resolve_radius_km(mandate.radius_km, facility["surveillance_radius_km"], settings_doc().get("surveillance_default_radius_km"))
	fix = parse_checkin(parse_payload(location))
	result = geofence.evaluate_checkin(fix, facility["latitude"], facility["longitude"], radius_km, setting_int("surveillance_max_accuracy_m"))
	return facility, fix, result


def open_session_for_mandate(mandate, user, location, device, start):
	"""Geofence check-in + session creation, shared by the QR-code path
	(start_session) and the officer queue path (accept_mandate)."""
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


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def accept_mandate(mandate_no, location=None, device=None):
	"""Officer queue path: accept a mandate assigned to the caller by its
	mandate number (list rows never expose the QR short code) and open the
	session with the same geofence check-in as the code path. An in-progress
	session for the mandate is resumed unchanged (idempotent)."""
	start = time.time()
	user = frappe.session.user
	mandate_no = str(mandate_no or "").strip()
	if not mandate_no:
		log_event(None, user, "error", "mandate", None, details={"reason": "mandate_no missing"}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest("A mandate_no is required.")
	rows = frappe.get_all("HISI Surveillance Mandate", filters={"name": mandate_no}, fields=MANDATE_FIELDS, limit=1)
	if not rows:
		log_event(None, user, "mandate_accept_failed", "mandate", mandate_no, outcome="denied", duration_ms=duration_ms(start))
		raise NotFound("Mandate not found.")
	mandate = rows[0]
	now = frappe.utils.now_datetime()
	expires_at = frappe.utils.get_datetime(mandate.expires_at)
	if expires_at and now > expires_at:
		frappe.db.set_value("HISI Surveillance Mandate", mandate.name, "status", "expired")
		log_event(None, user, "mandate_accept_failed", "mandate", mandate.mandate_no, details={"reason": "expired"}, outcome="denied", duration_ms=duration_ms(start))
		raise Forbidden("This mandate has expired.")
	if mandate.status == "revoked":
		log_event(None, user, "mandate_accept_failed", "mandate", mandate.mandate_no, details={"reason": "revoked"}, outcome="denied", duration_ms=duration_ms(start))
		raise MandateRevoked("The mandate has been revoked.")
	if mandate.status == "completed":
		log_event(None, user, "mandate_accept_failed", "mandate", mandate.mandate_no, details={"reason": "completed"}, outcome="denied", duration_ms=duration_ms(start))
		raise Conflict("This mandate is already completed.")
	valid_from = frappe.utils.get_datetime(mandate.valid_from)
	if valid_from and now < valid_from:
		log_event(None, user, "mandate_accept_failed", "mandate", mandate.mandate_no, details={"reason": "not yet valid"}, outcome="denied", duration_ms=duration_ms(start))
		raise Forbidden("This mandate is not active yet.")
	if mandate.assigned_officer != user:
		log_event(None, user, "auth_denied", "mandate", mandate.mandate_no, outcome="denied", duration_ms=duration_ms(start))
		raise Unauthorized("This mandate is not assigned to you.")
	# Idempotent: an open session for this mandate is resumed — an active one
	# unchanged; a paused one with a fresh geofence check-in. A lapsed
	# session is closed and a fresh one is opened below.
	open_sessions = frappe.get_all(
		"HISI Surveillance Session",
		filters={"mandate": mandate.name, "status": ["in", ["active", "paused"]]},
		fields=["name", "session_no", "status"],
		order_by="creation desc",
		limit=1,
	)
	if open_sessions:
		open_session = open_sessions[0]
		try:
			require_session(open_session.name, touch=False)
		except (Forbidden, MandateRevoked):
			frappe.db.set_value("HISI Surveillance Session", open_session.name, "status", "expired")
			log_event(open_session.name, user, "session_expired", "session", open_session.session_no, details={"reason": "lapsed_on_reaccept"}, outcome="denied", duration_ms=duration_ms(start))
		else:
			if open_session.status == "active":
				log_event(None, user, "mandate_accepted", "mandate", mandate.mandate_no, details={"resumed_session": open_session.session_no}, outcome="ok", duration_ms=duration_ms(start))
				return APIResponse.success(data={"session_no": open_session.session_no, "status": "active", "resumed": True, "mandate_no": mandate.mandate_no, "facility": mandate.facility})
			# Paused: resume with a fresh geofence check-in before continuing.
			facility, fix, result = geofence_for_mandate(mandate, location)
			settings = settings_doc()
			policy = "flag" if not settings.get("surveillance_strict_location") else (mandate.location_policy or "strict")
			if result.status != "verified" and policy == "strict":
				log_event(open_session.name, user, "checkin_failed", "facility", facility["name"], details={"status": result.status, "distance_m": result.distance_m, "flags": result.flags, "policy": policy}, outcome="denied", location=fix, duration_ms=duration_ms(start))
				raise LocationCheckFailed(checkin_failure_message(result))
			resumed_now = frappe.utils.now_datetime()
			apply_checkin(open_session, result, fix, device)
			frappe.db.set_value("HISI Surveillance Session", open_session.name, {"status": "active", "last_activity": resumed_now.strftime(DATA_FMT)})
			log_event(open_session.name, user, "session_resumed", "session", open_session.session_no, details={"source": "queue_reaccept"}, outcome="ok", duration_ms=duration_ms(start))
			log_event(None, user, "mandate_accepted", "mandate", mandate.mandate_no, details={"resumed_session": open_session.session_no, "was_paused": True}, outcome="ok", duration_ms=duration_ms(start))
			return APIResponse.success(data={"session_no": open_session.session_no, "status": "active", "resumed": True, "mandate_no": mandate.mandate_no, "facility": mandate.facility})
	log_event(None, user, "mandate_accepted", "mandate", mandate.mandate_no, outcome="ok", duration_ms=duration_ms(start))
	return open_session_for_mandate(mandate, user, location, device, start)


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
	return open_session_for_mandate(mandate, user, location, device, start)


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
				"summary": row["summary"],
				"attachments": session_attachments(row["name"]),
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
def pause_session(session_id):
	"""Pause an in-progress session: the idle clock stops (the officer may
	step away), the TTL still runs, and the queue shows the mandate as
	Paused until the officer resumes it."""
	start = time.time()
	user = frappe.session.user
	ctx = require_session(session_id, touch=False)
	row = ctx["session"]
	if row["status"] != "active":
		log_event(row["name"], user, "error", "session", row["session_no"], details={"reason": "not in progress", "status": row["status"]}, outcome="error", duration_ms=duration_ms(start))
		raise Conflict("Only an in-progress session can be paused.")
	frappe.db.set_value("HISI Surveillance Session", row["name"], "status", "paused")
	log_event(row["name"], user, "session_paused", "session", row["session_no"], outcome="ok", duration_ms=duration_ms(start))
	return APIResponse.success(data={"session_no": row["session_no"], "status": "paused"})


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def resume_session(session_id, location=None, device=None):
	"""Resume a paused session: a fresh GPS fix is validated against the
	facility geofence (same policy as the original check-in), the session
	returns to in progress, and the idle clock restarts from now."""
	start = time.time()
	user = frappe.session.user
	ctx = require_session(session_id, touch=False)
	row = ctx["session"]
	if row["status"] != "paused":
		log_event(row["name"], user, "error", "session", row["session_no"], details={"reason": "not paused", "status": row["status"]}, outcome="error", duration_ms=duration_ms(start))
		raise Conflict("Only a paused session can be resumed.")
	mandate = ctx["mandate"]
	facility, fix, result = geofence_for_mandate(mandate, location)
	settings = settings_doc()
	policy = "flag" if not settings.get("surveillance_strict_location") else (mandate.location_policy or "strict")
	if result.status != "verified" and policy == "strict":
		log_event(row["name"], user, "checkin_failed", "facility", facility["name"], details={"status": result.status, "distance_m": result.distance_m, "flags": result.flags, "policy": policy}, outcome="denied", location=fix, duration_ms=duration_ms(start))
		raise LocationCheckFailed(checkin_failure_message(result))
	now = frappe.utils.now_datetime()
	apply_checkin(row, result, fix, device)
	frappe.db.set_value("HISI Surveillance Session", row["name"], {"status": "active", "last_activity": now.strftime(DATA_FMT)})
	log_event(row["name"], user, "session_resumed", "session", row["session_no"], details={"distance_m": round(result.distance_m, 1) if result.distance_m is not None else None}, outcome="ok", duration_ms=duration_ms(start))
	return APIResponse.success(
		data={
			"session_no": row["session_no"],
			"status": "active",
			"location_status": result.status,
			"checkin_distance_m": round(result.distance_m, 1) if result.distance_m is not None else None,
		}
	)


# Findings recording: the officer records what the visit showed - free
# text plus uploaded evidence (documents and screenshots). The text lands
# in the session's summary field; evidence files attach to the session
# document as private File records. Ownership is the same as the session
# itself (require_session), so an officer can only ever record or fetch
# evidence for a session they are working.

ALLOWED_EVIDENCE_MIME = {
	"image/jpeg",
	"image/png",
	"image/webp",
	"image/gif",
	"image/heic",
	"image/heif",
	"application/pdf",
	"text/plain",
	"text/csv",
	"application/msword",
	"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
	"application/vnd.ms-excel",
	"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
MAX_FINDINGS_CHARS = 20000


def session_attachments(session_name):
	rows = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "HISI Surveillance Session", "attached_to_name": session_name},
		fields=["name", "file_name", "file_url", "file_size", "file_type"],
		order_by="creation asc",
	)
	return [
		{
			"name": row["name"],
			"file_name": row["file_name"],
			"file_url": row["file_url"],
			"file_size": row["file_size"],
			"mime_type": row["file_type"],
		}
		for row in rows
	]


def safe_upload_name(filename):
	name = os.path.basename(str(filename or "").replace("\\", "/"))
	name = re.sub(r"[^\w.\- ]+", "_", name).strip().strip(".")
	return name[:120] or "file"


def owned_session_row(doc_name):
	"""Session row for a File reference, owner-checked (any status, so
	   completed sessions keep their evidence retrievable)."""
	user = frappe.session.user
	rows = frappe.get_all(
		"HISI Surveillance Session",
		filters={"name": doc_name},
		fields=["name", "session_no", "user", "status"],
		limit=1,
	)
	if not rows:
		log_event(None, user, "error", "session", None, details={"reason": "file session missing"}, outcome="error")
		raise NotFound("The session this file belongs to is missing.")
	row = rows[0]
	if row["user"] != user:
		log_event(row["name"], user, "auth_denied", "session", row["session_no"], outcome="denied")
		raise Forbidden("You do not have access to this file.")
	return row


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def record_findings(session_id, summary=None):
	start = time.time()
	user = frappe.session.user
	ctx = require_session(session_id, touch=True)
	row = ctx["session"]
	text = str(summary or "").strip()
	if len(text) > MAX_FINDINGS_CHARS:
		raise BadRequest("Findings text is limited to {} characters.".format(MAX_FINDINGS_CHARS))
	frappe.db.set_value("HISI Surveillance Session", row["name"], "summary", text or None)
	log_event(row["name"], user, "findings_recorded", "session", row["session_no"], details={"length": len(text)}, outcome="ok", duration_ms=duration_ms(start))
	return APIResponse.success(data={"session_no": row["session_no"], "summary": text or None})


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=30, seconds=60)
@api_endpoint
def upload_evidence(session_id, filename, content_b64, mime_type=None):
	start = time.time()
	user = frappe.session.user
	ctx = require_session(session_id, touch=True)
	row = ctx["session"]
	mime = str(mime_type or "").strip().lower()
	if mime not in ALLOWED_EVIDENCE_MIME:
		log_event(row["name"], user, "evidence_rejected", "session", row["session_no"], details={"reason": "mime not allowed", "mime": mime}, outcome="denied", duration_ms=duration_ms(start))
		raise BadRequest("That file type is not allowed. Upload images, PDF, text, CSV or Office documents.")
	try:
		raw = base64.b64decode(str(content_b64 or ""), validate=True)
	except (ValueError, TypeError):
		raise BadRequest("The file content could not be decoded.")
	if not raw:
		raise BadRequest("The file is empty.")
	if len(raw) > MAX_EVIDENCE_BYTES:
		log_event(row["name"], user, "evidence_rejected", "session", row["session_no"], details={"reason": "too large", "bytes": len(raw)}, outcome="denied", duration_ms=duration_ms(start))
		raise BadRequest("Evidence files are limited to 10 MB.")
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": safe_upload_name(filename),
			"is_private": 1,
			"attached_to_doctype": "HISI Surveillance Session",
			"attached_to_name": row["name"],
		}
	)
	file_doc.content = raw
	file_doc.flags.new_file = True
	file_doc.save_file(content=raw)
	file_doc.insert(ignore_permissions=True)
	log_event(row["name"], user, "evidence_uploaded", "session", row["session_no"], details={"file": file_doc.file_name, "bytes": len(raw)}, outcome="ok", duration_ms=duration_ms(start))
	return APIResponse.success(
		data={
			"session_no": row["session_no"],
			"file": {
				"name": file_doc.name,
				"file_name": file_doc.file_name,
				"file_url": file_doc.file_url,
				"file_size": file_doc.file_size,
				"mime_type": file_doc.file_type,
			},
		}
	)


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def get_evidence(file_name):
	start = time.time()
	user = frappe.session.user
	name = str(file_name or "").strip()
	if not name:
		raise BadRequest("A file_name is required.")
	meta = frappe.db.get_value(
		"File", name, ["attached_to_doctype", "attached_to_name", "file_name", "file_type", "file_size"], as_dict=True
	)
	if not meta or meta["attached_to_doctype"] != "HISI Surveillance Session":
		log_event(None, user, "error", "file", name, details={"reason": "file not found"}, outcome="error", duration_ms=duration_ms(start))
		raise NotFound("The file was not found.")
	row = owned_session_row(meta["attached_to_name"])
	content = frappe.get_doc("File", name).get_content()
	if not isinstance(content, bytes):
		content = str(content).encode("utf-8")
	log_event(row["name"], user, "evidence_downloaded", "session", row["session_no"], details={"file": meta["file_name"]}, outcome="ok", duration_ms=duration_ms(start))
	return APIResponse.success(
		data={
			"file_name": meta["file_name"],
			"mime_type": meta["file_type"],
			"file_size": meta["file_size"],
			"content_b64": base64.b64encode(content).decode("ascii"),
		}
	)


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
		filters={"status": ["in", ["active", "paused"]]},
		fields=["name", "session_no", "user", "status", "ends_at", "last_activity"],
	)
	expired = 0
	for row in rows:
		ends_at = frappe.utils.get_datetime(row["ends_at"]) if row["ends_at"] else None
		last_activity = frappe.utils.get_datetime(row["last_activity"]) if row["last_activity"] else None
		reason = None
		if ends_at and now > ends_at:
			reason = "ttl"
		elif row["status"] == "active" and idle_minutes and last_activity and now > last_activity + timedelta(minutes=idle_minutes):
			reason = "idle"
		if reason:
			frappe.db.set_value("HISI Surveillance Session", row["name"], "status", "expired")
			log_event(row["name"], row["user"], "session_expired", "session", row["session_no"], details={"reason": reason}, outcome="denied")
			expired += 1
	return expired
