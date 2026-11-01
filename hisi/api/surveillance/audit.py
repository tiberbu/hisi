import json
import re
import time

import frappe
from frappe.rate_limiter import rate_limit

from hisi.api.response_handler import APIResponse, BadRequest, Forbidden, NotFound, api_endpoint
from hisi.utils import active_companies, is_company_member


def mask_ref(value):
	if value is None or value == "":
		return None
	text = str(value)
	if len(text) <= 4:
		return "****"
	if len(text) <= 8:
		return text[:2] + "\u2026" + text[-2:]
	return text[:5] + "\u2026" + text[-2:]


def request_ip():
	try:
		return frappe.request.ip if frappe.request else None
	except Exception:
		return None


def log_event(
	session,
	user,
	event_type,
	target_type=None,
	target_ref=None,
	details=None,
	outcome="ok",
	location=None,
	duration_ms=None,
):
	if session is None or session == "":
		session = None
	if user is None:
		user = frappe.session.user
	try:
		frappe.get_doc(
			{
				"doctype": "HISI Surveillance Event",
				"session": session,
				"company": _event_company(session, user),
				"user": user,
				"event_type": event_type,
				"target_type": target_type or None,
				"target_ref_masked": mask_ref(target_ref),
				"details": json.dumps(details) if details is not None else None,
				"ip": request_ip(),
				"location": json.dumps(location) if location is not None else None,
				"outcome": outcome,
				"duration_ms": duration_ms,
			}
		).insert(ignore_permissions=True)
		return True
	except Exception as exc:
		try:
			frappe.log_error(message=str(exc) or repr(exc), title="Surveillance audit insert failed")
		except Exception:
			pass
		if session:
			try:
				# raw db read/write on purpose: appends the flag without a full doc round-trip
				# (field renamed flags -> checkin_flags because 'flags' collided with
				# Frappe's reserved Document.flags attribute and serialized the internal dict)
				current = frappe.db.get_value("HISI Surveillance Session", session, "checkin_flags")
				flags = json.loads(current or "[]")
				if "audit_degraded" not in flags:
					flags.append("audit_degraded")
					frappe.db.set_value("HISI Surveillance Session", session, "checkin_flags", json.dumps(flags))
			except Exception:
				pass
		return False


def _duration_ms(start):
	return int((time.time() - start) * 1000)


def _payer_companies(user):
	return active_companies(user)


def _event_company(session, user):
	"""The Company owning the audited record: the session's company when the
	event sits on a session, otherwise the caller's first payer company (a
	null-company row stays visible to the platform in Desk only)."""
	company = None
	if session:
		company = frappe.db.get_value("HISI Surveillance Session", session, "company")
	if not company:
		orgs = active_companies(user)
		if orgs:
			company = orgs[0]
	return company or None


def _facility_view(facility_name):
	rows = frappe.get_all(
		"HISI Health Facility",
		filters={"name": facility_name},
		fields=["name", "facility_code", "county", "level", "operational_status", "license", "latitude", "longitude"],
		limit=1,
	)
	if not rows:
		return None
	row = rows[0]
	return {
		"name": row["name"],
		"facility_code": row["facility_code"],
		"county": row["county"],
		"level": row["level"],
		"operational_status": row["operational_status"],
		"license": row["license"],
		"latitude": row["latitude"],
		"longitude": row["longitude"],
	}


def _practitioner_views(facility_name):
	rows = frappe.get_all(
		"HISI Professional Record",
		filters={"facility": facility_name},
		fields=["name", "cadre", "regulator", "registration_number", "facility"],
		order_by="name asc",
	)
	return [
		{
			"record_id": row["name"],
			"name": row["name"],
			"cadre": row["cadre"],
			"regulator": row["regulator"],
			"registration_number": row["registration_number"],
			"facility": row["facility"],
		}
		for row in rows
	]


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=120, seconds=60)
@api_endpoint
def list_sessions(page=None, page_size=None, status=None, location_status=None):
	start = time.time()
	user = frappe.session.user
	try:
		number = int(page) if page not in (None, "") else 1
	except (TypeError, ValueError):
		raise BadRequest("page must be a positive integer.")
	if number < 1:
		raise BadRequest("page must be a positive integer.")
	page = number
	try:
		number = int(page_size) if page_size not in (None, "") else 20
	except (TypeError, ValueError):
		raise BadRequest("page_size must be a positive integer.")
	if number < 1:
		raise BadRequest("page_size must be a positive integer.")
	page_size = min(number, 100)
	filters = {}
	if status not in (None, ""):
		filters["status"] = str(status).strip()
	if location_status not in (None, ""):
		filters["location_status"] = str(location_status).strip()
	orgs = _payer_companies(user)
	if orgs:
		scope_kind = "payer"
		mandate_names = frappe.get_all("HISI Surveillance Mandate", filters={"company": ["in", orgs]}, pluck="name")
		filters["mandate"] = ["in", mandate_names or [""]]
	else:
		# Officer scope: an officer who is not a payer member sees their own
		# sessions (the findings trail of the mandates they worked).
		if "HISI Insurance Surveillance" not in frappe.get_roles(user):
			log_event(None, user, "scope_denied", "session", None, outcome="denied", duration_ms=_duration_ms(start))
			raise Forbidden("You are not a member of a payer organization.")
		scope_kind = "officer"
		filters["user"] = user
	rows = frappe.get_all(
		"HISI Surveillance Session",
		filters=filters,
		fields=["name", "session_no", "mandate", "user", "status", "location_status", "checkin_distance_m", "checkin_flags", "started_at", "ends_at", "summary"],
		order_by="creation desc",
	)
	total = len(rows)
	total_pages = (total + page_size - 1) // page_size if total else 0
	lo = (page - 1) * page_size
	page_rows = rows[lo : lo + page_size]
	mandate_names = [row["mandate"] for row in page_rows if row["mandate"]]
	facilities = {}
	if mandate_names:
		for row in frappe.get_all("HISI Surveillance Mandate", filters={"name": ["in", mandate_names]}, fields=["name", "facility"]):
			facilities[row["name"]] = row["facility"]
	attachment_counts = {}
	session_names = [row["name"] for row in page_rows if row["name"]]
	if session_names:
		for row in frappe.get_all(
			"File",
			filters={"attached_to_doctype": "HISI Surveillance Session", "attached_to_name": ["in", session_names]},
			fields=["attached_to_name"],
		):
			attachment_counts[row["attached_to_name"]] = attachment_counts.get(row["attached_to_name"], 0) + 1
	sessions = []
	for row in page_rows:
		sessions.append(
			{
				"session_no": row["session_no"],
				"mandate_no": row["mandate"],
				"officer": row["user"],
				"facility_name": facilities.get(row["mandate"]),
				"status": row["status"],
				"location_status": row["location_status"],
				"checkin_distance_m": row["checkin_distance_m"],
				"flags": json.loads(row["checkin_flags"] or "[]"),
				"summary_excerpt": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row["summary"] or "")).strip()[:160] or None,
				"attachment_count": attachment_counts.get(row["name"], 0),
				"started_at": str(row["started_at"]) if row["started_at"] else None,
				"ends_at": str(row["ends_at"]) if row["ends_at"] else None,
			}
		)
	log_event(None, user, "sessions_listed", "session", None, details={"scope": scope_kind, "total": total, "page": page, "page_size": page_size}, outcome="ok", duration_ms=_duration_ms(start))
	return APIResponse.success(data={"sessions": sessions, "page": page, "page_size": page_size, "total": total, "total_pages": total_pages})


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=120, seconds=60)
@api_endpoint
def list_mandates(page=None, page_size=None, status=None):
	start = time.time()
	user = frappe.session.user
	try:
		number = int(page) if page not in (None, "") else 1
	except (TypeError, ValueError):
		raise BadRequest("page must be a positive integer.")
	if number < 1:
		raise BadRequest("page must be a positive integer.")
	page = number
	try:
		number = int(page_size) if page_size not in (None, "") else 20
	except (TypeError, ValueError):
		raise BadRequest("page_size must be a positive integer.")
	if number < 1:
		raise BadRequest("page_size must be a positive integer.")
	page_size = min(number, 100)
	filters = {}
	if status not in (None, ""):
		filters["status"] = str(status).strip()
	orgs = _payer_companies(user)
	if orgs:
		scope_kind = "payer"
		filters["company"] = ["in", orgs]
	else:
		# Officer scope: a surveillance officer who is not a payer member sees
		# the mandates dispatched to them (the queue they accept from).
		if "HISI Insurance Surveillance" not in frappe.get_roles(user):
			log_event(None, user, "scope_denied", "mandate", None, outcome="denied", duration_ms=_duration_ms(start))
			raise Forbidden("You are not a member of a payer organization.")
		scope_kind = "officer"
		filters["assigned_officer"] = user
	rows = frappe.get_all(
		"HISI Surveillance Mandate",
		filters=filters,
		fields=["name", "facility", "assigned_officer", "status", "reason", "valid_from", "expires_at", "creation"],
		order_by="creation desc",
	)
	total = len(rows)
	total_pages = (total + page_size - 1) // page_size if total else 0
	lo = (page - 1) * page_size
	page_rows = rows[lo : lo + page_size]
	scope_by_mandate = {}
	if page_rows:
		for line in frappe.get_all(
			"HISI Surveillance Scope Line",
			filters={"parent": ["in", [row["name"] for row in page_rows]], "parenttype": "HISI Surveillance Mandate"},
			fields=["parent", "line_type", "patient_identifier", "visit_id", "note"],
		):
			entry = {"line_type": line["line_type"]}
			if line["line_type"] == "patient":
				entry["patient_ref_masked"] = mask_ref(line["patient_identifier"])
			else:
				entry["visit_ref_masked"] = mask_ref(line["visit_id"])
			if line["note"]:
				entry["note"] = line["note"]
			scope_by_mandate.setdefault(line["parent"], []).append(entry)
	# Per-mandate session state for the queue: the newest session decides
	# the row's next step (in progress / paused / completed / closed / none).
	session_state_by_mandate = {}
	if page_rows:
		for row in frappe.get_all(
			"HISI Surveillance Session",
			filters={"mandate": ["in", [r["name"] for r in page_rows]]},
			fields=["mandate", "status", "creation"],
			order_by="creation desc",
		):
			if row["mandate"] not in session_state_by_mandate:
				state_map = {"active": "in_progress", "paused": "paused", "completed": "completed", "expired": "closed", "revoked": "closed"}
				session_state_by_mandate[row["mandate"]] = state_map.get(row["status"], "none")
	mandates = []
	for row in page_rows:
		lines = scope_by_mandate.get(row["name"], [])
		mandates.append(
			{
				"mandate_no": row["name"],
				"facility": row["facility"],
				"facility_name": row["facility"],
				"assigned_officer": row["assigned_officer"],
				"status": row["status"],
				"session_state": session_state_by_mandate.get(row["name"], "none"),
				"scope_counts": {
					"patient": sum(1 for entry in lines if entry["line_type"] == "patient"),
					"visit": sum(1 for entry in lines if entry["line_type"] != "patient"),
				},
				"scope_lines": lines,
				"reason": row["reason"],
				"valid_from": str(row["valid_from"]) if row["valid_from"] else None,
				"expires_at": str(row["expires_at"]) if row["expires_at"] else None,
				"created_on": str(row["creation"]) if row["creation"] else None,
			}
		)
	log_event(None, user, "mandate_listed", "mandate", None, details={"scope": scope_kind, "total": total, "page": page, "page_size": page_size}, outcome="ok", duration_ms=_duration_ms(start))
	return APIResponse.success(data={"mandates": mandates, "page": page, "page_size": page_size, "total": total, "total_pages": total_pages})


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=120, seconds=60)
@api_endpoint
def get_session_audit(session_id):
	start = time.time()
	user = frappe.session.user
	session_id = str(session_id or "").strip()
	if not session_id:
		log_event(None, user, "error", "session", None, details={"reason": "session_id missing"}, outcome="error", duration_ms=_duration_ms(start))
		raise BadRequest("A session_id is required.")
	rows = frappe.get_all(
		"HISI Surveillance Session",
		filters={"session_no": session_id},
		fields=["name", "session_no", "mandate", "user", "status", "started_at", "ends_at", "location_status", "checkin_distance_m", "checkin_accuracy_m", "checkin_flags", "surveiled_refs", "summary"],
		limit=1,
	)
	if not rows:
		log_event(None, user, "error", "session", session_id, details={"reason": "session not found"}, outcome="error", duration_ms=_duration_ms(start))
		raise NotFound("Session not found.")
	row = rows[0]
	mandate_rows = frappe.get_all("HISI Surveillance Mandate", filters={"name": row["mandate"]}, fields=["name", "mandate_no", "company", "facility", "valid_from", "expires_at"], limit=1)
	if not mandate_rows:
		log_event(row["name"], user, "error", "session", row["session_no"], details={"reason": "mandate missing"}, outcome="error", duration_ms=_duration_ms(start))
		raise NotFound("The session mandate is missing.")
	mandate = mandate_rows[0]
	if not is_company_member(mandate.company, user):
		log_event(row["name"], user, "auth_denied", "session", row["session_no"], outcome="denied", duration_ms=_duration_ms(start))
		raise Forbidden("You do not have access to this session audit.")
	events = []
	for event in frappe.get_all(
		"HISI Surveillance Event",
		filters={"session": row["name"]},
		fields=["event_type", "user", "outcome", "target_type", "target_ref_masked", "details", "location", "duration_ms", "creation"],
		order_by="creation asc, name asc",
	):
		details = event["details"]
		if details:
			try:
				details = json.loads(details)
			except ValueError:
				pass
		events.append(
			{
				"event_type": event["event_type"],
				"user": event["user"],
				"outcome": event["outcome"],
				"target_type": event["target_type"],
				"target_ref_masked": event["target_ref_masked"],
				"details": details,
				"location": event["location"],
				"duration_ms": event["duration_ms"],
				"creation": str(event["creation"]) if event["creation"] else None,
			}
		)
	data = {
		"session": {
			"session_no": row["session_no"],
			"status": row["status"],
			"mandate_no": mandate.mandate_no,
			"officer": row["user"],
			"company": mandate.company,
			"facility": mandate.facility,
			"started_at": str(row["started_at"]) if row["started_at"] else None,
			"ends_at": str(row["ends_at"]) if row["ends_at"] else None,
			"location_status": row["location_status"],
			"checkin_distance_m": row["checkin_distance_m"],
			"checkin_accuracy_m": row["checkin_accuracy_m"],
			"flags": json.loads(row["checkin_flags"] or "[]"),
			"surveiled_refs": json.loads(row["surveiled_refs"] or "[]"),
			"summary": row["summary"],
		},
		"events": events,
		"surveiled_targets": {
			"facility": _facility_view(mandate.facility),
			"practitioners": _practitioner_views(mandate.facility),
		},
		"export": {"format": "json", "generated_at": frappe.utils.now()},
	}
	log_event(row["name"], user, "audit_viewed", "session", row["session_no"], details={"event_count": len(events)}, outcome="ok", duration_ms=_duration_ms(start))
	return APIResponse.success(data=data)
