"""Field surveillance (case-based) endpoints.

Two roles, one data model (the design language's "same data, two roles"):
HQ admins (``HISI Insurance Provider`` / platform) create cases, review
findings, close or return them and decide reassignment requests; field
officers (``HISI Insurance Surveillance`` / platform) work the cases
assigned to them. Every state change lands in the case's activity trail.
All scoping and permission checks run here, server-side."""

import base64
import json
import os
import re

import frappe
from frappe.rate_limiter import rate_limit

from hisi.api.response_handler import APIResponse, BadRequest, Conflict, Forbidden, NotFound, api_endpoint
from hisi.api.patient import http_get
from hisi.api.fs import common
from hisi.api.surveillance.facilities import facility_search_url
from hisi.utils import OFFICER_ROLE

CATEGORIES = ("Claim", "Patient visit", "Overall facility")
PRIORITIES = ("Urgent", "High", "Medium", "Low")
REASON_CATEGORIES = ("Conflict of interest", "Workload", "Distance or safety", "Lack of expertise", "Other")
OUTCOMES = ("Substantiated", "Partially substantiated", "Not substantiated")

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
# File.file_type stores the uppercase extension, not a mime type - the
# preview dialog needs the real one, so map the allowed extensions back.
EXTENSION_MIME = {
	"JPG": "image/jpeg",
	"JPEG": "image/jpeg",
	"PNG": "image/png",
	"WEBP": "image/webp",
	"GIF": "image/gif",
	"HEIC": "image/heic",
	"HEIF": "image/heif",
	"PDF": "application/pdf",
	"TXT": "text/plain",
	"CSV": "text/csv",
	"DOC": "application/msword",
	"DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
	"XLS": "application/vnd.ms-excel",
	"XLSX": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def mime_for_file(file_doc):
	ext = str(getattr(file_doc, "file_type", "") or "").strip().upper()
	return EXTENSION_MIME.get(ext, "application/octet-stream")


MAX_SCOPE_ITEMS = 20
MAX_STEPS = 12
MAX_FINDINGS_CHARS = 20000


def as_list(raw):
	if raw in (None, ""):
		return None
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			return None
	return raw if isinstance(raw, list) else None


def clean_scope_items(raw):
	items = as_list(raw)
	if not items:
		raise BadRequest("Add at least one scope item.")
	if len(items) > MAX_SCOPE_ITEMS:
		raise BadRequest("A case can cover at most {} scope items.".format(MAX_SCOPE_ITEMS))
	cleaned = []
	for entry in items:
		if not isinstance(entry, dict):
			raise BadRequest("Each scope item needs a type, a reference and a facility.")
		item_type = str(entry.get("item_type") or "").strip()
		reference = str(entry.get("reference") or "").strip()
		display_name = str(entry.get("display_name") or "").strip()
		facility = str(entry.get("facility") or "").strip()
		if item_type not in ("claim", "patient", "facility"):
			raise BadRequest("Each scope item type must be claim, patient or facility.")
		if not reference:
			raise BadRequest("Each scope item needs its reference (claim ID, patient identifier or facility).")
		if not facility:
			raise BadRequest("Each scope item needs its facility.")
		cleaned.append(
			{
				"item_type": item_type,
				"reference": reference,
				"display_name": display_name or reference,
				"facility": facility,
			}
		)
	return cleaned


def clean_steps(raw):
	"""The wizard's step checklist (template-derived, edited per case).
	Returns None when the caller sends nothing (the template is used)."""
	if raw in (None, ""):
		return None
	steps = as_list(raw)
	if steps is None:
		return None
	if not steps:
		raise BadRequest("Keep at least one step.")
	if len(steps) > MAX_STEPS:
		raise BadRequest("A case can hold at most {} steps.".format(MAX_STEPS))
	cleaned = []
	for entry in steps:
		text = str((entry or {}).get("text") or "").strip() if isinstance(entry, dict) else str(entry).strip()
		if not text:
			raise BadRequest("Every step needs text.")
		cleaned.append(text)
	return cleaned


def template_for_category(category):
	rows = frappe.get_all(
		"HISI Surveillance Template",
		filters={"category": category},
		fields=["name"],
		limit=1,
	)
	if not rows:
		return None
	return frappe.get_doc("HISI Surveillance Template", rows[0].name)


def require_officer_user(email):
	email = str(email or "").strip()
	if not email:
		raise BadRequest("Choose a field officer.")
	enabled = frappe.db.get_value("User", email, "enabled")
	if not enabled:
		raise BadRequest("That account is not an active surveillance officer.")
	if OFFICER_ROLE not in set(frappe.get_roles(email)):
		raise BadRequest("That user does not hold the HISI Insurance Surveillance role.")
	return email


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=300, seconds=60)
@api_endpoint
def list_cases(status=None, category=None, page=None, page_size=None):
	"""The case list with its summary tiles. HQ sees every case; an officer
	sees the cases assigned to them (the server decides the scope). Paged:
	the tiles summarize the whole filtered set, `cases` carries only the
	requested page, and total/page/page_size drive the client pager."""
	user = frappe.session.user
	scope = common.scope_kind(user)
	filters = {}
	if status not in (None, ""):
		status = str(status).strip()
		if status not in (common.STATUS_ASSIGNED, common.STATUS_ACCEPTED, common.STATUS_IN_PROGRESS, common.STATUS_FINDINGS_SUBMITTED, common.STATUS_CLOSED):
			raise BadRequest("Unknown status filter.")
		filters["status"] = status
	if category not in (None, ""):
		category = str(category).strip()
		if category not in CATEGORIES:
			raise BadRequest("Unknown category filter.")
		filters["category"] = category
	if scope == "officer":
		filters["assigned_officer"] = user
	try:
		page = max(1, int(page or 1))
	except (TypeError, ValueError):
		page = 1
	try:
		page_size = int(page_size or 10)
	except (TypeError, ValueError):
		page_size = 10
	page_size = min(max(page_size, 1), 100)
	rows = frappe.get_all(
		"HISI Surveillance Case",
		filters=filters,
		fields=["name", "case_no", "title", "category", "priority", "due_date", "status", "field_office", "assigned_officer", "creation"],
		order_by="creation desc",
		limit=1000,
	)
	# One batch query for the pending reassignment flags (list label + tile).
	pending_by_case = {}
	# One batch query for step progress (done/total per case) — the list rows
	# do not carry child tables.
	step_counts = {}
	if rows:
		case_names = [row.name for row in rows]
		for row in frappe.get_all(
			"HISI Surveillance Reassignment",
			filters={"case": ["in", case_names], "status": "pending"},
			fields=["case"],
		):
			pending_by_case[row.case] = True
		for step in frappe.get_all(
			"HISI Surveillance Case Step",
			filters={"parent": ["in", case_names]},
			fields=["parent", "done"],
		):
			done, total = step_counts.get(step.parent, (0, 0))
			step_counts[step.parent] = (done + (1 if step.done else 0), total + 1)
	all_cases = []
	for row in rows:
		done, total = step_counts.get(row.name, (0, 0))
		progress = {"done": done, "total": total, "percent": int(round(100.0 * done / total)) if total else 0}
		all_cases.append(common.case_list_row(row, pending_by_case.get(row.name), progress))
	summary = common.list_summary(rows)
	summary["reassignment_requests"] = sum(pending_by_case.values())
	start = (page - 1) * page_size
	return APIResponse.success(
		data={
			"cases": all_cases[start:start + page_size],
			"counts": summary,
			"scope": scope,
			"total": len(all_cases),
			"page": page,
			"page_size": page_size,
		}
	)


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=300, seconds=60)
@api_endpoint
def get_case(case_no):
	"""Full case view: header facts, scope, steps, evidence, findings,
	reassignment state, the activity trail and the caller's viewer role."""
	user = frappe.session.user
	doc = common.require_case(case_no)
	if common.is_hq(user):
		scope = "hq"
	else:
		if not common.is_officer(user):
			raise Forbidden("This surface is restricted to surveillance roles.")
		if doc.assigned_officer != user:
			raise Forbidden("This case is not assigned to you.")
		scope = "officer"
	return APIResponse.success(data=common.case_view(doc, scope, doc.assigned_officer == user))


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def get_attachment(case_no, file_name):
	"""The file bytes (base64) of a case attachment for the preview dialog.
	The caller must be able to view the case, and the File must actually be
	attached to it (a checklist step or the findings) - arbitrary File docs
	are never served."""
	user = frappe.session.user
	doc = common.require_case(case_no)
	if not common.is_hq(user):
		if not common.is_officer(user):
			raise Forbidden("This surface is restricted to surveillance roles.")
		if doc.assigned_officer != user:
			raise Forbidden("This case is not assigned to you.")
	targets = []
	for step in doc.get("steps") or []:
		targets.extend(common.attachment_list(step.attachments))
	targets.extend(common.attachment_list(doc.findings_attachments))
	if not any(row.get("name") == file_name for row in targets):
		raise NotFound("Attachment not found on this case.")
	if not frappe.db.exists("File", file_name):
		raise NotFound("Attachment not found on this case.")
	file_doc = frappe.get_doc("File", file_name)
	content = file_doc.get_content()
	raw = content if isinstance(content, bytes) else str(content).encode("utf-8")
	if len(raw) > MAX_EVIDENCE_BYTES:
		raise BadRequest("Files over 10 MB cannot be previewed.")
	return APIResponse.success(
		data={
			"file_name": file_doc.file_name,
			"mime_type": mime_for_file(file_doc),
			"file_size": file_doc.file_size,
			"content_b64": base64.b64encode(raw).decode("ascii"),
		}
	)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def create_case(category, title, description=None, priority=None, due_date=None, scope_items=None, steps=None, assigned_officer=None, officer_notes=None):
	"""HQ creates a case from the wizard: category, problem, scope, the
	edited step checklist and the assigned officer. Steps default to the
	category template when the caller sends none. The case starts assigned."""
	user = frappe.session.user
	common.require_hq(user)
	category = str(category or "").strip()
	if category not in CATEGORIES:
		raise BadRequest("Choose a category.")
	title = str(title or "").strip()
	if not title:
		raise BadRequest("Add a title.")
	priority = str(priority or "Medium").strip()
	if priority not in PRIORITIES:
		raise BadRequest("Choose a priority.")
	if not due_date:
		raise BadRequest("Choose a due date.")
	try:
		frappe.utils.getdate(due_date)
	except (TypeError, ValueError):
		raise BadRequest("The due date is not a valid date.")
	items = clean_scope_items(scope_items)
	officer = require_officer_user(assigned_officer)
	step_texts = clean_steps(steps)
	if step_texts is None:
		template = template_for_category(category)
		step_texts = [str(step.text).strip() for step in (template.get("template_steps") or [])] if template else []
	if not step_texts:
		raise BadRequest("The template for this category has no steps; add at least one.")
	case = frappe.get_doc(
		{
			"doctype": "HISI Surveillance Case",
			"case_no": common.next_case_no(),
			"category": category,
			"title": title,
			"description": str(description or "").strip() or None,
			"priority": priority,
			"due_date": str(due_date),
			"status": common.STATUS_ASSIGNED,
			"field_office": common.officer_office(officer),
			"assigned_officer": officer,
			"officer_notes": str(officer_notes or "").strip() or None,
			"created_by": user,
			"scope_items": items,
			"steps": [{"step_key": "s{}".format(index + 1), "text": text, "sort": index, "done": 0} for index, text in enumerate(step_texts)],
		}
	)
	common.log_case_activity(case, "case_created", "Case created by {} and assigned to {} ({}).".format(user, officer, priority))
	case.insert(ignore_permissions=True)
	return APIResponse.success(data=common.case_view(case, "hq", False))


def _transition(doc, expected, target, action, details):
	if doc.status != expected:
		raise Conflict("The case is {} - this action needs the {} state.".format(doc.status, expected))
	doc.status = target
	common.log_case_activity(doc, action, details)
	doc.save(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def accept_case(case_no):
	"""Officer accepts the mandate: assigned → accepted."""
	user = frappe.session.user
	doc = common.require_officer_case(case_no, user)
	_transition(doc, common.STATUS_ASSIGNED, common.STATUS_ACCEPTED, "status_accepted", "Accepted the case.")
	return APIResponse.success(data=common.case_view(doc, "officer", True))


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def start_fieldwork(case_no):
	"""Officer starts fieldwork: accepted → in progress."""
	user = frappe.session.user
	doc = common.require_officer_case(case_no, user)
	_transition(doc, common.STATUS_ACCEPTED, common.STATUS_IN_PROGRESS, "status_in_progress", "Started fieldwork.")
	return APIResponse.success(data=common.case_view(doc, "officer", True))


def _rich_text_of(html):
	"""Plain-text view of a rich-HTML note, for required-checks and logs."""
	return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(html or ""))).strip()


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def update_step(case_no, step_key, done, note=None):
	"""Officer ticks (or unticks) a checklist step while in progress. Ticking
	requires details describing what was done - a rich-HTML note (stored as-is
	on the step) plus optional attachments; the plain-text view is quoted in
	the activity trail."""
	user = frappe.session.user
	doc = common.require_officer_case(case_no, user)
	if doc.status != common.STATUS_IN_PROGRESS:
		raise Conflict("Steps can only be updated while the case is in progress.")
	step_key = str(step_key or "").strip()
	step = next((row for row in doc.get("steps") or [] if row.step_key == step_key), None)
	if step is None:
		raise NotFound("Step not found on this case.")
	flag = 1 if done in (True, 1, "1", "true", "True") else 0
	html = str(note or "").strip()
	if html in ("", "<p></p>", "<p><br></p>", "<p><br/></p>"):
		html = ""
	text = _rich_text_of(html)
	attachments = common.attachment_list(step.attachments)
	if flag and not text and not attachments:
		raise BadRequest("Add details (some text or an attachment) before marking this step complete.")
	step.done = flag
	if flag:
		step.note = html or None
		step.done_by = user
		step.done_on = frappe.utils.now()
		summary = text[:200] if text else "attachment(s) only"
		common.log_case_activity(doc, "step_done", "Ticked: {} — {}".format(step.text, summary))
	else:
		step.note = None
		step.done_by = None
		step.done_on = None
		common.log_case_activity(doc, "step_undone", "Unticked: {}".format(step.text))
	doc.save(ignore_permissions=True)
	progress = common.case_progress(doc)
	return APIResponse.success(data={"step_key": step_key, "done": bool(flag), "note": html or None, "progress": progress})


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def upload_step_attachment(case_no, step_key, filename, content_b64, mime_type=None):
	"""Officer uploads one file attached to a checklist step while in progress;
	stored private against the case and tracked in the step's attachment list."""
	user = frappe.session.user
	doc = common.require_officer_case(case_no, user)
	if doc.status != common.STATUS_IN_PROGRESS:
		raise Conflict("Attachments can only be added while the case is in progress.")
	step_key = str(step_key or "").strip()
	step = next((row for row in doc.get("steps") or [] if row.step_key == step_key), None)
	if step is None:
		raise NotFound("Step not found on this case.")
	file_doc = _store_case_file(doc, filename, content_b64, mime_type, "Attachments")
	attachments = common.attachment_list(step.attachments)
	attachments.append(_attachment_row(file_doc))
	step.attachments = attachments
	common.log_case_activity(doc, "step_attachment_added", "Attached {} to step {}.".format(file_doc.file_name, step.text))
	doc.save(ignore_permissions=True)
	return APIResponse.success(data={"attachments": attachments})


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def remove_step_attachment(case_no, step_key, file_name):
	"""Officer removes a file attached to a checklist step while in progress."""
	user = frappe.session.user
	doc = common.require_officer_case(case_no, user)
	if doc.status != common.STATUS_IN_PROGRESS:
		raise Conflict("Attachments can only be removed while the case is in progress.")
	step_key = str(step_key or "").strip()
	step = next((row for row in doc.get("steps") or [] if row.step_key == step_key), None)
	if step is None:
		raise NotFound("Step not found on this case.")
	attachments = common.attachment_list(step.attachments)
	target = next((a for a in attachments if a.get("name") == file_name), None)
	if target is None:
		raise NotFound("Attachment not found on this step.")
	removed_name = target.get("file_name")
	step.attachments = [a for a in attachments if a.get("name") != file_name]
	_delete_attachment_file(file_name)
	common.log_case_activity(doc, "step_attachment_removed", "Removed {} from step {}.".format(removed_name, step.text))
	doc.save(ignore_permissions=True)
	return APIResponse.success(data={"attachments": step.attachments})


def _store_case_file(doc, filename, content_b64, mime_type, label):
	"""Validate + store one private file against the case, return the File doc."""
	mime = str(mime_type or "").strip().lower()
	if mime not in ALLOWED_EVIDENCE_MIME:
		raise BadRequest("That file type is not allowed. Upload images, PDF, text, CSV or Office documents.")
	try:
		raw = base64.b64decode(str(content_b64 or ""), validate=True)
	except (ValueError, TypeError):
		raise BadRequest("The file content could not be decoded.")
	if not raw:
		raise BadRequest("The file is empty.")
	if len(raw) > MAX_EVIDENCE_BYTES:
		raise BadRequest("{} are limited to 10 MB.".format(label))
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": safe_upload_name(filename),
			"is_private": 1,
			"attached_to_doctype": "HISI Surveillance Case",
			"attached_to_name": doc.name,
			# content must be in the doc at construction time: with no file_url
			# and no content, File.is_remote_file is True and save_file returns
			# without writing, so insert() would fail in validate_file_on_disk.
			"content": raw,
		}
	)
	file_doc.insert(ignore_permissions=True)
	return file_doc


def _attachment_row(file_doc):
	return {
		"name": file_doc.name,
		"file_name": file_doc.file_name,
		"file_url": file_doc.file_url,
		"mime_type": file_doc.file_type,
		"file_size": file_doc.file_size,
		"uploaded_on": frappe.utils.now(),
	}


def _delete_attachment_file(file_name):
	try:
		frappe.delete_doc("File", file_name, ignore_permissions=True)
	except Exception:
		# Keep the metadata removal authoritative even if disk cleanup fails.
		frappe.logger("hisi.fs").warning("Could not delete File doc %s", file_name)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def upload_findings_attachment(case_no, filename, content_b64, mime_type=None):
	"""Officer attaches an optional file to the findings draft while in
	progress; the files travel with the findings when they are submitted."""
	user = frappe.session.user
	doc = common.require_officer_case(case_no, user)
	if doc.status != common.STATUS_IN_PROGRESS:
		raise Conflict("Findings attachments can only be added while the case is in progress.")
	file_doc = _store_case_file(doc, filename, content_b64, mime_type, "Attachments")
	attachments = common.attachment_list(doc.findings_attachments)
	attachments.append(_attachment_row(file_doc))
	doc.findings_attachments = attachments
	common.log_case_activity(doc, "findings_attachment_added", "Attached {} to the findings.".format(file_doc.file_name))
	doc.save(ignore_permissions=True)
	return APIResponse.success(data={"attachments": attachments})


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def remove_findings_attachment(case_no, file_name):
	"""Officer removes a file from the findings draft while in progress."""
	user = frappe.session.user
	doc = common.require_officer_case(case_no, user)
	if doc.status != common.STATUS_IN_PROGRESS:
		raise Conflict("Findings attachments can only be removed while the case is in progress.")
	attachments = common.attachment_list(doc.findings_attachments)
	target = next((a for a in attachments if a.get("name") == file_name), None)
	if target is None:
		raise NotFound("Attachment not found on the findings.")
	removed_name = target.get("file_name")
	doc.findings_attachments = [a for a in attachments if a.get("name") != file_name]
	_delete_attachment_file(file_name)
	common.log_case_activity(doc, "findings_attachment_removed", "Removed {} from the findings.".format(removed_name))
	doc.save(ignore_permissions=True)
	return APIResponse.success(data={"attachments": doc.findings_attachments})


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=30, seconds=60)
@api_endpoint
def upload_evidence(case_no, filename, content_b64, mime_type=None, note=None):
	"""Officer uploads one evidence file (base64 over the JSON proxy) while
	in progress; the file is stored private and attached to the case."""
	user = frappe.session.user
	doc = common.require_officer_case(case_no, user)
	if doc.status != common.STATUS_IN_PROGRESS:
		raise Conflict("Evidence can only be uploaded while the case is in progress.")
	mime = str(mime_type or "").strip().lower()
	if mime not in ALLOWED_EVIDENCE_MIME:
		raise BadRequest("That file type is not allowed. Upload images, PDF, text, CSV or Office documents.")
	try:
		raw = base64.b64decode(str(content_b64 or ""), validate=True)
	except (ValueError, TypeError):
		raise BadRequest("The file content could not be decoded.")
	if not raw:
		raise BadRequest("The file is empty.")
	if len(raw) > MAX_EVIDENCE_BYTES:
		raise BadRequest("Evidence files are limited to 10 MB.")
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": safe_upload_name(filename),
			"is_private": 1,
			"attached_to_doctype": "HISI Surveillance Case",
			"attached_to_name": doc.name,
			"content": raw,
		}
	)
	file_doc.insert(ignore_permissions=True)
	doc.append(
		"evidence",
		{
			"file": file_doc.name,
			"file_name": file_doc.file_name,
			"file_url": file_doc.file_url,
			"mime_type": file_doc.file_type,
			"file_size": file_doc.file_size,
			"note": str(note or "").strip() or None,
			"uploaded_by": user,
			"uploaded_on": frappe.utils.now(),
		},
	)
	common.log_case_activity(doc, "evidence_added", "Uploaded {} ({}).".format(file_doc.file_name, mime))
	doc.save(ignore_permissions=True)
	return APIResponse.success(
		data={
			"file": {
				"name": file_doc.name,
				"file_name": file_doc.file_name,
				"file_url": file_doc.file_url,
				"file_size": file_doc.file_size,
				"mime_type": file_doc.file_type,
			},
			"evidence": common.evidence_payload(doc),
		}
	)


def safe_upload_name(filename):
	name = os.path.basename(str(filename or "").replace("\\", "/"))
	name = re.sub(r"[^\w.\- ]+", "_", name).strip().strip(".")
	return name[:120] or "file"


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def save_findings(case_no, outcome=None, action=None, summary=None):
	"""Officer saves the findings draft (outcome, recommended action,
	summary) while in progress. Submitting is a separate, gated call."""
	user = frappe.session.user
	doc = common.require_officer_case(case_no, user)
	if doc.status != common.STATUS_IN_PROGRESS:
		raise Conflict("Findings can only be saved while the case is in progress.")
	outcome = str(outcome or "").strip() or None
	if outcome and outcome not in OUTCOMES:
		raise BadRequest("Unknown outcome.")
	doc.findings_outcome = outcome
	doc.findings_action = str(action or "").strip() or None
	summary = str(summary or "").strip()
	if len(summary) > MAX_FINDINGS_CHARS:
		raise BadRequest("Findings summary is limited to {} characters.".format(MAX_FINDINGS_CHARS))
	doc.findings_summary = summary or None
	common.log_case_activity(doc, "findings_saved", "Saved findings ({}).".format(outcome or "no outcome yet"))
	doc.save(ignore_permissions=True)
	return APIResponse.success(
		data={
			"findings": {
				"outcome": doc.findings_outcome,
				"action": doc.findings_action,
				"summary": doc.findings_summary,
			}
		}
	)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def submit_findings(case_no):
	"""Officer submits the findings: in progress → findings submitted.
	Blocked until an outcome and a summary exist (the error names the fix)."""
	user = frappe.session.user
	doc = common.require_officer_case(case_no, user)
	if doc.status != common.STATUS_IN_PROGRESS:
		raise Conflict("Findings can only be submitted while the case is in progress.")
	if not doc.findings_outcome:
		raise BadRequest("Choose an outcome before submitting.")
	if not str(doc.findings_summary or "").strip():
		raise BadRequest("Write a summary before submitting.")
	_transition(doc, common.STATUS_IN_PROGRESS, common.STATUS_FINDINGS_SUBMITTED, "status_findings_submitted", "Submitted findings ({}) for HQ review.".format(doc.findings_outcome))
	return APIResponse.success(data=common.case_view(doc, "officer", True))


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def close_case(case_no):
	"""HQ approves the findings and closes the case: findings submitted
	→ closed. Reserved for HISI Insurance Provider users."""
	user = frappe.session.user
	common.require_provider(user)
	doc = common.require_case(case_no)
	if doc.status != common.STATUS_FINDINGS_SUBMITTED:
		raise Conflict("Only submitted findings can be closed.")
	doc.status = common.STATUS_CLOSED
	doc.closed_by = user
	doc.closed_on = frappe.utils.now()
	common.log_case_activity(doc, "status_closed", "Approved the findings and closed the case.")
	doc.save(ignore_permissions=True)
	return APIResponse.success(data=common.case_view(doc, "hq", doc.assigned_officer == user))


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def return_for_rework(case_no):
	"""HQ returns the case for rework: findings submitted → in progress.
	The officer re-submits when done."""
	user = frappe.session.user
	common.require_hq(user)
	doc = common.require_case(case_no)
	if doc.status != common.STATUS_FINDINGS_SUBMITTED:
		raise Conflict("Only submitted findings can be returned for rework.")
	_transition(doc, common.STATUS_FINDINGS_SUBMITTED, common.STATUS_IN_PROGRESS, "status_returned", "Returned for rework by {}.".format(user))
	return APIResponse.success(data=common.case_view(doc, "hq", doc.assigned_officer == user))


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def request_reassignment(case_no, reason_category, explanation):
	"""Officer asks for the case to be reassigned: a reason category and a
	written explanation, both required. One pending request per case."""
	user = frappe.session.user
	doc = common.require_officer_case(case_no, user)
	if doc.status == common.STATUS_CLOSED:
		raise Conflict("Closed cases cannot be reassigned.")
	if common.pending_reassignment(doc.name):
		raise Conflict("A reassignment request is already pending for this case.")
	reason_category = str(reason_category or "").strip()
	if reason_category not in REASON_CATEGORIES:
		raise BadRequest("Choose a reason category.")
	explanation = str(explanation or "").strip()
	if not explanation:
		raise BadRequest("Explain why the case should be reassigned.")
	request = frappe.get_doc(
		{
			"doctype": "HISI Surveillance Reassignment",
			"case": doc.name,
			"requested_by": user,
			"reason_category": reason_category,
			"explanation": explanation,
			"status": "pending",
		}
	)
	common.log_case_activity(doc, "reassignment_requested", "Requested reassignment ({}).".format(reason_category))
	request.insert(ignore_permissions=True)
	doc.save(ignore_permissions=True)
	return APIResponse.success(data=common.reassignment_payload(doc))


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def decide_reassignment(case_no, decision, new_officer=None, reply=None):
	"""HQ decides the pending request: ``reassign`` (pick another officer;
	the case goes back to assigned so the new officer must accept it; steps
	and evidence are kept) or ``decline`` (a reply to the officer is
	required; they keep the case)."""
	user = frappe.session.user
	common.require_hq(user)
	doc = common.require_case(case_no)
	pending = common.pending_reassignment(doc.name)
	if not pending:
		raise NotFound("There is no pending reassignment request on this case.")
	request = frappe.get_doc("HISI Surveillance Reassignment", pending[0].name)
	decision = str(decision or "").strip()
	if decision == "reassign":
		officer = require_officer_user(new_officer)
		request.status = "reassigned"
		request.new_officer = officer
		request.resolved_by = user
		request.resolved_on = frappe.utils.now()
		request.save(ignore_permissions=True)
		doc.assigned_officer = officer
		doc.status = common.STATUS_ASSIGNED
		doc.field_office = common.officer_office(officer)
		common.log_case_activity(doc, "reassignment_reassigned", "Reassigned to {} (was {}). The new officer must accept the case.".format(officer, request.requested_by))
		doc.save(ignore_permissions=True)
	elif decision == "decline":
		reply = str(reply or "").strip()
		if not reply:
			raise BadRequest("Write a reply to the officer before declining.")
		request.status = "declined"
		request.reply = reply
		request.resolved_by = user
		request.resolved_on = frappe.utils.now()
		request.save(ignore_permissions=True)
		common.log_case_activity(doc, "reassignment_declined", "Declined the reassignment request; {} keeps the case.".format(request.requested_by))
		doc.save(ignore_permissions=True)
	else:
		raise BadRequest("The decision must be reassign or decline.")
	return APIResponse.success(data=common.case_view(doc, "hq", doc.assigned_officer == user))


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=120, seconds=60)
@api_endpoint
def list_officers():
	"""HQ: the field officer picker data - the onboarded officers (an active
	Employee record carrying the surveillance designation, the same source
	the dispatch flow's confirmed list uses) instead of every account that
	happens to hold the role, with office, active case count and the
	counties their active cases touch (the wizard groups matching officers
	first)."""
	user = frappe.session.user
	common.require_hq(user)
	onboarded = []
	for row in frappe.get_all(
		"Employee",
		filters={"status": "Active", "designation": OFFICER_ROLE},
		fields=["user_id"],
	):
		if row.user_id and row.user_id not in onboarded:
			onboarded.append(row.user_id)
	officers = []
	if onboarded:
		users = frappe.get_all("User", filters={"name": ["in", onboarded], "enabled": 1}, fields=["name", "full_name"], order_by="full_name asc")
		for row in users:
			email = row.name
			# Onboarding grants the role; an account that lost it cannot work
			# cases, so it stays out of the picker.
			if OFFICER_ROLE not in set(frappe.get_roles(email)):
				continue
			active_cases = frappe.get_all(
				"HISI Surveillance Case",
				filters={"assigned_officer": email, "status": ["in", common.ACTIVE_STATUSES]},
				fields=["name"],
			)
			counties = set()
			for case in active_cases:
				for item in frappe.get_all(
					"HISI Surveillance Case Scope Item",
					filters={"parent": case.name, "parenttype": "HISI Surveillance Case"},
					fields=["facility"],
				):
					if item.facility:
						county = frappe.db.get_value("HISI Health Facility", item.facility, "county")
						if county:
							counties.add(county)
			officers.append(
				{
					"user": email,
					"full_name": row.full_name or email,
					"office": common.officer_office(email),
					"active_case_count": len(active_cases),
					"counties": sorted(counties),
				}
			)
	return APIResponse.success(data={"officers": officers, "total": len(officers)})


@frappe.whitelist(methods=["GET", "POST"])
@rate_limit(limit=120, seconds=60)
@api_endpoint
def search_facilities(term):
	"""Type-ahead over the live DHA facility register (the same HWR
	``Health Facility`` resource the mandate flow searches) for the
	wizard's scope step. Each register row carries the name plus the
	facility identifiers (code / fid), so rows that share a name stay
	distinguishable in the picker and the options can key on a unique id."""
	user = frappe.session.user
	common.scope_kind(user)
	term = str(term or "").strip()
	if len(term) < 2:
		return APIResponse.success(data={"facilities": [], "total": 0})
	if len(term) > 100:
		raise BadRequest("The search term is too long ({} characters).".format(len(term)))
	params = {
		"filters": json.dumps([["facility_name", "like", "%{}%".format(term)]]),
		"fields": json.dumps(["*"]),
		"order_by": "facility_name asc",
		"limit_page_length": 20,
		"limit_start": 0,
	}
	payload = http_get(facility_search_url(), params)
	rows = payload.get("data") if isinstance(payload, dict) else payload
	if not isinstance(rows, list):
		rows = []
	facilities = [
		{
			"facility_name": row.get("facility_name"),
			"facility_code": row.get("facility_code") or None,
			"facility_fid": row.get("facility_fid") or None,
			"county": row.get("county") or None,
			"level": row.get("facility_level") or None,
			"operational_status": row.get("operational_status") or None,
		}
		for row in rows
		if isinstance(row, dict) and row.get("facility_name")
	]
	return APIResponse.success(data={"facilities": facilities, "total": len(facilities)})


def template_payload(doc):
	return {
		"category": doc.category,
		"total_days": doc.total_days or 0,
		"steps": [
			{"text": step.text, "target_days": step.target_days, "sort": step.sort or 0}
			for step in sorted(doc.get("template_steps") or [], key=lambda row: row.sort or 0)
		],
		"note_templates": [
			{"name": note.template_name, "text": note.text} for note in (doc.get("note_templates") or [])
		],
	}


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=120, seconds=60)
@api_endpoint
def list_templates():
	"""The one template per category (step timeline + note templates)."""
	user = frappe.session.user
	common.scope_kind(user)
	docs = frappe.get_all("HISI Surveillance Template", fields=["name"], order_by="name asc")
	templates = [template_payload(frappe.get_doc("HISI Surveillance Template", row.name)) for row in docs]
	return APIResponse.success(data={"templates": templates, "total": len(templates)})


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=30, seconds=60)
@api_endpoint
def save_template(category, steps, note_templates=None):
	"""HQ edits the template for one category (the Surveillance templates
	screen). Edits apply to new requests only - existing cases keep the
	steps and notes they were created with. At least one step must remain."""
	user = frappe.session.user
	common.require_hq(user)
	category = str(category or "").strip()
	if category not in CATEGORIES:
		raise BadRequest("Unknown category.")
	steps = as_list(steps)
	if not steps:
		raise BadRequest("Keep at least one step.")
	if len(steps) > MAX_STEPS:
		raise BadRequest("A template can hold at most {} steps.".format(MAX_STEPS))
	cleaned_steps = []
	for index, entry in enumerate(steps):
		if not isinstance(entry, dict):
			raise BadRequest("Each step needs text and target days.")
		text = str(entry.get("text") or "").strip()
		if not text:
			raise BadRequest("Every step needs text.")
		try:
			target_days = int(entry.get("target_days"))
		except (TypeError, ValueError):
			raise BadRequest("Each step needs a target number of days.")
		if target_days < 1:
			raise BadRequest("Target days must be at least 1.")
		cleaned_steps.append({"text": text, "target_days": target_days, "sort": index})
	cleaned_notes = []
	notes = as_list(note_templates)
	if notes:
		for entry in notes:
			if not isinstance(entry, dict):
				raise BadRequest("Each note template needs a name and text.")
			name = str(entry.get("name") or "").strip()
			if not name:
				raise BadRequest("Each note template needs a name.")
			cleaned_notes.append({"template_name": name, "text": str(entry.get("text") or "").strip()})
	rows = frappe.get_all("HISI Surveillance Template", filters={"category": category}, fields=["name"], limit=1)
	if rows:
		doc = frappe.get_doc("HISI Surveillance Template", rows[0].name)
		doc.template_steps = []
		doc.note_templates = []
		for step in cleaned_steps:
			doc.append("template_steps", step)
		for note in cleaned_notes:
			doc.append("note_templates", note)
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({"doctype": "HISI Surveillance Template", "category": category})
		for step in cleaned_steps:
			doc.append("template_steps", step)
		for note in cleaned_notes:
			doc.append("note_templates", note)
		doc.insert(ignore_permissions=True)
	return APIResponse.success(data=template_payload(doc))
