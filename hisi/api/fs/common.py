"""Shared helpers for the field surveillance (case-based) API.

Role model (server-side, the real authority):
- HQ admin: ``HISI Insurance Provider`` (or a platform role) - creates
  cases, reviews findings, closes/returns, decides reassignments, manages
  templates.
- Field office: ``HISI Insurance Surveillance`` (or a platform role) -
  works the cases assigned to them: accept, start, tick steps, evidence,
  findings, request reassignment.
The client gate in the frontend mirrors this only for presentation."""

import time

import frappe

from hisi.api.response_handler import Forbidden
from hisi.utils import OFFICER_ROLE, PROVIDER_ROLE, is_platform_user

STATUS_ASSIGNED = "assigned"
STATUS_ACCEPTED = "accepted"
STATUS_IN_PROGRESS = "in_progress"
STATUS_FINDINGS_SUBMITTED = "findings_submitted"
STATUS_CLOSED = "closed"

ACTIVE_STATUSES = [STATUS_ASSIGNED, STATUS_ACCEPTED, STATUS_IN_PROGRESS, STATUS_FINDINGS_SUBMITTED]


def duration_ms(start):
	return int((time.time() - start) * 1000)


def request_ip():
	try:
		return frappe.request.ip if frappe.request else None
	except Exception:
		return None


def user_roles(user=None):
	user = user or frappe.session.user
	return set(frappe.get_roles(user)) if user and user != "Guest" else set()


def is_hq(user=None):
	return is_platform_user(user) or PROVIDER_ROLE in user_roles(user)


def is_officer(user=None):
	return is_platform_user(user) or OFFICER_ROLE in user_roles(user)


def scope_kind(user=None):
	"""Which list scope the caller gets: ``hq`` (every case) or ``officer``
	(their own cases). Raises Forbidden for users with neither role."""
	user = user or frappe.session.user
	if is_hq(user):
		return "hq"
	if is_officer(user):
		return "officer"
	raise Forbidden("This surface is restricted to surveillance roles.")


def require_hq(user=None):
	if not is_hq(user):
		raise Forbidden("This action requires the HISI Insurance Provider role.")


def require_officer(user=None):
	if not is_officer(user):
		raise Forbidden("This action requires the HISI Insurance Surveillance role.")


def case_row(case_no):
	rows = frappe.db.get_all(
		"HISI Surveillance Case",
		filters={"case_no": str(case_no or "").strip()},
		fields=["name", "case_no", "category", "title", "priority", "due_date", "status", "creation", "modified"],
		limit=1,
	)
	return rows[0] if rows else None


def case_doc(case_no):
	row = case_row(case_no)
	if row is None:
		return None
	return frappe.get_doc("HISI Surveillance Case", row.name)


def require_case(case_no):
	doc = case_doc(case_no)
	if doc is None:
		from hisi.api.response_handler import NotFound

		raise NotFound("Case not found.")
	return doc


def require_officer_case(case_no, user=None):
	"""Case doc for an officer action: the caller must be the assignee (or a
	platform user, who bypasses the assignment)."""
	user = user or frappe.session.user
	doc = require_case(case_no)
	if not is_platform_user(user) and doc.assigned_officer != user:
		raise Forbidden("This case is not assigned to you.")
	return doc


def next_case_no():
	year = frappe.utils.now_datetime().year
	prefix = "FS-{}-".format(year)
	existing = frappe.get_all("HISI Surveillance Case", filters={"case_no": ["like", prefix + "%"]}, pluck="case_no")
	sequence = 0
	for number in existing:
		tail = str(number)[len(prefix):]
		if tail.isdigit():
			sequence = max(sequence, int(tail))
	for _ in range(5):
		candidate = "{}{:04d}".format(prefix, sequence + 1)
		if not frappe.db.exists("HISI Surveillance Case", {"case_no": candidate}):
			return candidate
		sequence += 1
	# Extremely unlikely: reuse a collision-safe suffix.
	return "{}{:04d}".format(prefix, sequence + 1)


def log_case_activity(case, action, details, actor=None):
	"""Append one row to the case's activity trail. ``case`` is the case
	Document; the row persists with the caller's next ``case.save()``."""
	user = actor or frappe.session.user
	full_name = (frappe.db.get_value("User", user, "full_name") or "").strip()
	case.append(
		"activity",
		{
			"action": action,
			"details": str(details),
			"actor": user,
			"actor_name": full_name or user,
			"created_on": frappe.utils.now(),
		},
	)


def officer_full_name(officer):
	if not officer:
		return None
	return (frappe.db.get_value("User", officer, "full_name") or "").strip() or officer


def officer_office(officer):
	"""The officer's field office: the name of their first active Company
	(fallback: None)."""
	if not officer:
		return None
	row = frappe.db.get_all(
		"Employee",
		filters={"user_id": officer, "status": "Active"},
		fields=["company"],
		order_by="creation asc",
		limit=1,
	)
	if not row:
		return None
	return frappe.db.get_value("Company", row[0].company, "company_name")


def case_progress(case):
	done = 0
	total = 0
	for step in case.get("steps") or []:
		total += 1
		if step.get("done"):
			done += 1
	return {"done": done, "total": total, "percent": int(round(100.0 * done / total)) if total else 0}


def due_date_days(case_due_date):
	"""Days from today to the due date (negative when past due)."""
	try:
		due = frappe.utils.getdate(case_due_date)
	except (TypeError, ValueError):
		return None
	return (due - frappe.utils.getdate(frappe.utils.nowdate())).days


def is_overdue(case):
	if case.status == STATUS_CLOSED:
		return False
	days = due_date_days(case.due_date)
	return days is not None and days < 0


def pending_reassignment(case_name):
	return frappe.db.get_all(
		"HISI Surveillance Reassignment",
		filters={"case": case_name, "status": "pending"},
		fields=["name", "requested_by", "reason_category", "explanation", "creation"],
		order_by="creation desc",
		limit=1,
	)


def latest_reassignment(case_name):
	return frappe.db.get_all(
		"HISI Surveillance Reassignment",
		filters={"case": case_name},
		fields=["name", "requested_by", "reason_category", "explanation", "status", "reply", "new_officer", "resolved_on", "creation"],
		order_by="creation desc",
		limit=1,
	)


def scope_items_payload(case):
	items = []
	for item in case.get("scope_items") or []:
		items.append(
			{
				"item_type": item.item_type,
				"reference": item.reference,
				"display_name": item.display_name,
				"facility": item.facility,
			}
		)
	return items


def facilities_of(case):
	"""Distinct facility names across the case's scope items."""
	seen = []
	for item in case.get("scope_items") or []:
		if item.facility and item.facility not in seen:
			seen.append(item.facility)
	return seen


def steps_payload(case):
	steps = []
	for step in case.get("steps") or []:
		steps.append(
			{
				"step_key": step.step_key,
				"text": step.text,
				"sort": step.sort or 0,
				"done": bool(step.done),
				"done_by": step.done_by,
				"done_on": str(step.done_on) if step.done_on else None,
			}
		)
	steps.sort(key=lambda row: (row["sort"], row["step_key"]))
	return steps


def evidence_payload(case):
	rows = []
	for row in case.get("evidence") or []:
		rows.append(
			{
				"file": row.file,
				"file_name": row.file_name,
				"file_url": row.file_url,
				"mime_type": row.mime_type,
				"file_size": row.file_size,
				"note": row.note,
				"uploaded_by": row.uploaded_by,
				"uploaded_on": str(row.uploaded_on) if row.uploaded_on else None,
			}
		)
	rows.sort(key=lambda row: row["uploaded_on"] or "")
	return rows


def activity_payload(case, newest_first=True):
	rows = []
	for row in case.get("activity") or []:
		rows.append(
			{
				"action": row.action,
				"details": row.details,
				"actor": row.actor,
				"actor_name": row.actor_name,
				"created_on": str(row.created_on) if row.created_on else None,
			}
		)
	if newest_first:
		rows.sort(key=lambda row: row["created_on"] or "", reverse=True)
	return rows


def reassignment_payload(case):
	row = latest_reassignment(case.name)
	if not row:
		return None
	row = row[0]
	return {
		"name": row.name,
		"requested_by": row.requested_by,
		"requested_by_name": officer_full_name(row.requested_by),
		"reason_category": row.reason_category,
		"explanation": row.explanation,
		"status": row.status,
		"reply": row.reply,
		"new_officer": row.new_officer,
		"resolved_on": str(row.resolved_on) if row.resolved_on else None,
		"created_on": str(row.creation) if row.creation else None,
	}


def case_view(case, viewer_scope, is_assignee):
	"""Full case payload (get_case + create_case confirmation)."""
	progress = case_progress(case)
	days = due_date_days(case.due_date)
	officer = case.assigned_officer
	return {
		"case": {
			"case_no": case.case_no,
			"title": case.title,
			"category": case.category,
			"description": case.description,
			"priority": case.priority,
			"due_date": str(case.due_date) if case.due_date else None,
			"status": case.status,
			"field_office": case.field_office,
			"assigned_officer": officer,
			"officer_name": officer_full_name(officer),
			"officer_notes": case.officer_notes,
			"days_left": days,
			"overdue": is_overdue(case),
			"progress": progress,
			"scope_items": scope_items_payload(case),
			"facilities": facilities_of(case),
			"steps": steps_payload(case),
			"evidence": evidence_payload(case),
			"findings": {
				"outcome": case.findings_outcome,
				"action": case.findings_action,
				"summary": case.findings_summary,
			},
			"reassignment": reassignment_payload(case),
			"closed_by": case.closed_by,
			"closed_on": str(case.closed_on) if case.closed_on else None,
			"created_on": str(case.creation) if case.creation else None,
		},
		"activity": activity_payload(case),
		"viewer": {"role": viewer_scope, "is_assignee": bool(is_assignee)},
	}


def case_list_row(case, pending_reopen, progress=None):
	if progress is None:
		progress = case_progress(case)
	days = due_date_days(case.due_date)
	officer = case.assigned_officer
	return {
		"case_no": case.case_no,
		"title": case.title,
		"category": case.category,
		"priority": case.priority,
		"due_date": str(case.due_date) if case.due_date else None,
		"status": case.status,
		"field_office": case.field_office,
		"assigned_officer": officer,
		"officer_name": officer_full_name(officer),
		"progress": progress,
		"days_left": days,
		"overdue": is_overdue(case),
		"reassignment_requested": bool(pending_reopen),
		"created_on": str(case.creation) if case.creation else None,
	}


def list_summary(rows):
	overdue = 0
	awaiting = 0
	findings = 0
	for row in rows:
		if is_overdue(row):
			overdue += 1
		if row.status == STATUS_ASSIGNED:
			awaiting += 1
		if row.status == STATUS_FINDINGS_SUBMITTED:
			findings += 1
	return {
		"open": sum(1 for row in rows if row.status != STATUS_CLOSED),
		"overdue": overdue,
		"awaiting_acceptance": awaiting,
		"findings_to_review": findings,
	}
