import re

import frappe
from frappe.model.workflow import WorkflowTransitionError, apply_workflow

from hisi.checklist import (
	assessments_of,
	case_context,
	criteria_of,
	open_criteria,
	step_graph,
	steps_of,
	status_completion,
)

try:
	from hisi.api.response_handler import APIResponse, api_endpoint, BadRequest, Unauthorized, Forbidden, NotFound, Conflict, UpstreamError
except ImportError as er:
	raise str(er)

ADMIN_ROLE = "System Manager"
PASS_ASSESSMENTS = ("Pass", "Pass with observation")
STATUS_JUDGEMENT_TYPES = ("Approve", "Reject", "Request More Info", "Other")
STATUS_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
EVIDENCE_TYPES = ("Note", "File", "Note or file")

MAX_LISTED_CASES = 200
MAX_NOTE_LENGTH = 280
DEFAULT_VENDOR_PAGE_SIZE = 20
MAX_VENDOR_PAGE_SIZE = 100
MAX_VENDOR_FILTER_LENGTH = 64
MAX_STATUS_COUNT = 50
MAX_STATUS_LABEL_LENGTH = 140
MAX_STATUS_DESCRIPTION_LENGTH = 500
MAX_ACTION_LABEL_LENGTH = 64
MAX_STEP_COUNT = 12
MAX_SUB_STEP_COUNT = 20
MAX_EVIDENCE_HINT_LENGTH = 200
MAX_SORT_ORDER = 9999
MAX_DISPLAY_NAME_LENGTH = 140
MAX_CRITERION_TITLE_LENGTH = 140
MAX_CRITERION_STATEMENT_LENGTH = 2000
MAX_CRITERION_EVIDENCE_OPTIONS_LENGTH = 1000
PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9\s\-()]{4,19}$")
GENDER_OPTIONS = ("Male", "Female")

def require_platform() -> str:
	"""Return the session user, or throw when not a System Manager."""
	user = frappe.session.user
	if user == "Guest" or ADMIN_ROLE not in frappe.get_roles(user):
		raise BadRequest("Not authorized")
	return user
def _rollup(rows) -> dict:
	"""Criterion roll-up for one case: totals and pass/fail/pending counts."""
	total = len(rows)
	failed = sum(1 for row in rows if row.assessment == "Fail")
	blocked = sum(1 for row in rows if row.assessment == "Blocked")
	passed = sum(1 for row in rows if row.assessment in PASS_ASSESSMENTS)
	assessed = sum(1 for row in rows if row.assessment and row.assessment != "Not assessed")
	pending = total - assessed
	return {
		"total": total,
		"assessed": assessed,
		"passed": passed,
		"failed": failed,
		"blocked": blocked,
		"pending": pending,
	}

def _live_assessment_rows(case_name: str, checklist_version: str | None, rows=None) -> list:
	if rows is None:
		rows = assessments_of(case_name)
	else:
		rows = list(rows)
	if not checklist_version:
		return rows
	live = {row.criterion_id for row in criteria_of(checklist_version)}
	return [row for row in rows if row.criterion_id in live]

def _display_maps() -> tuple[dict, dict, dict]:
	vendor_names = {
		row.name: row.organization_name
		for row in frappe.get_all("HISI Vendor Organization", fields=["name", "organization_name"])
	}
	product_names = {
		row.name: row.product_name
		for row in frappe.get_all("HISI HMIS Product", fields=["name", "product_name"])
	}
	certificate_ids = {
		row.name: row.certificate_id
		for row in frappe.get_all("HISI DHA Certification Reference", fields=["name", "certificate_id"])
	}
	return vendor_names, product_names, certificate_ids

@frappe.whitelist()
def list_validation_cases() -> dict:
	require_platform()
	cases = frappe.get_all(
		"HISI SHA Validation Case",
		fields=[
			"name",
			"case_name",
			"vendor",
			"product",
			"dha_certificate",
			"release_version",
			"scope",
			"state",
			"current_owner",
			"next_action",
			"acknowledged_by",
			"acknowledged_on",
			"acknowledgement_note",
			"modified",
		],
		order_by="modified desc",
		limit_page_length=MAX_LISTED_CASES,
	)
	if not cases:
		return {"cases": []}
	vendor_names, product_names, certificate_ids = _display_maps()
	assessments: dict[str, list] = {}
	for row in frappe.get_all(
		"HISI Case Criterion Assessment",
		filters={"case": ["in", [case.name for case in cases]]},
		fields=["case", "criterion_id", "assessment"],
	):
		assessments.setdefault(row.case, []).append(row)
	contexts: dict[str, dict] = {}
	progress_by_case: dict[str, dict] = {}
	for row in frappe.get_all(
		"HISI Case Criterion Step Progress",
		filters={"case": ["in", [case.name for case in cases]]},
		fields=["case", "step", "sub_step", "reopened"],
	):
		progress_by_case.setdefault(row.case, {})[(row.step, row.sub_step or "")] = row

	items = []
	for case in cases:
		if case.checklist_version not in contexts:
			ordered_ids = [
				criterion.criterion_id for criterion in criteria_of(case.checklist_version)
			]
			contexts[case.checklist_version] = (step_graph(case.checklist_version), ordered_ids)
		graph, ordered_ids = contexts[case.checklist_version]
		assess_rows = _live_assessment_rows(
			case.name, case.checklist_version, assessments.get(case.name)
		)
		steps = _rollup(assess_rows)
		# Ready when every defined validation step is complete for every
		# criterion that still exists on the checklist.
		steps_ok = not open_criteria(
			graph, ordered_ids, progress_by_case.get(case.name, {})
		)
		items.append(
			{
				"caseId": case.name,
				"caseName": case.case_name,
				"vendorName": vendor_names.get(case.vendor) or case.vendor,
				"productName": product_names.get(case.product) or case.product,
				"certificateId": certificate_ids.get(case.dha_certificate) or case.dha_certificate,
				"releaseVersion": case.release_version or "",
				"scope": case.scope or "",
				"state": case.state,
				"currentOwner": case.current_owner or "",
				"nextAction": case.next_action or "",
				"steps": steps,
				# Ready when every criterion is assessed, none failed, and all
				# defined validation steps are complete.
				"canAcknowledge": bool(steps["total"]) and not steps["pending"] and not steps["failed"]
				and steps_ok and case.state != "Validation passed",
				"acknowledgedBy": case.acknowledged_by or "",
				"acknowledgedOn": str(case.acknowledged_on)[:16] if case.acknowledged_on else "",
				"acknowledgementNote": case.acknowledgement_note or "",
				"modified": str(case.modified)[:16] if case.modified else "",
			}
		)
	return {"cases": items}

@frappe.whitelist()
def acknowledge_validation(case_id: str, note: str = "") -> dict:
	user = require_platform()
	if not case_id:
		frappe.throw("case_id is required", frappe.ValidationError)

	case = frappe.get_doc("HISI SHA Validation Case", case_id)
	steps = _rollup(_live_assessment_rows(case.name, case.checklist_version))
	if not steps["total"] or steps["pending"] or steps["failed"]:
		frappe.throw("Case is not ready for acknowledgement", frappe.ValidationError)
	context = case_context(case.name, case.checklist_version)
	open_steps = open_criteria(context["graph"], context["ordered_ids"], context["progress"])
	if open_steps:
		frappe.throw(
			f"Validation steps are still open for: {', '.join(open_steps)}.",
			frappe.ValidationError,
		)
	try:
		case = apply_workflow(case, "Record platform acknowledgement")
	except WorkflowTransitionError:
		frappe.throw(
			f"Case {case.name} cannot be acknowledged from its current state ({case.state}).",
			frappe.ValidationError,
		)
	case.acknowledged_by = user
	case.acknowledged_on = frappe.utils.now()
	case.acknowledgement_note = (note or "").strip()[:MAX_NOTE_LENGTH]
	case.next_action = "Platform validation acknowledged"
	case.save()
	frappe.db.commit()

	return {
		"caseId": case.name,
		"state": case.state,
		"acknowledgedBy": user,
		"acknowledgedOn": str(case.acknowledged_on)[:16],
	}

def current_checklist():
	fields = ["name", "version", "effective_on", "published", "enabled", "release_notes"]
	published = frappe.get_all(
		"HISI Checklist Version",
		filters={"published": 1},
		fields=fields,
		order_by="effective_on desc, modified desc",
	)
	if published:
		return next((row for row in published if getattr(row, "enabled", 1)), published[0])
	latest = frappe.get_all(
		"HISI Checklist Version", fields=fields, order_by="modified desc", limit_page_length=1
	)
	return latest[0] if latest else None

def checklist_payload(row, criteria) -> dict:
	return {
		"version": row.version if row else "",
		"effectiveOn": (row.effective_on if row else None) or "",
		"published": int(row.published or 0) if row else 0,
		"enabled": int(getattr(row, "enabled", 1) or 0) if row else 0,
		"releaseNotes": (row.release_notes if row else None) or "",
		"criteria": criteria,
		"changes": _change_log_payload(row.name if row else None),
	}

def _change_log_payload(version_name: str | None, limit: int = 50) -> list[dict]:
	"""Change-history entries for one checklist version, newest first.

	Ordered by the row's own ``changed_on`` — ``modified`` is useless here
	because saving the version re-saves every child row and refreshes theirs,
	so all entries end up sharing one timestamp.
	"""
	if not version_name:
		return []
	items = []
	for row in frappe.get_all(
		"HISI Checklist Change Log",
		filters={"parenttype": "HISI Checklist Version", "parent": version_name},
		fields=["name", "changed_by", "changed_on", "summary", "changes"],
		order_by="changed_on desc, idx desc",
		limit_page_length=limit,
	):
		raw = {}
		if row.changes:
			try:
				parsed = frappe.parse_json(row.changes)
				if isinstance(parsed, dict):
					raw = parsed
			except Exception:
				raw = {}
		items.append(
			{
				"id": row.name,
				"changedBy": row.changed_by or "",
				"changedOn": str(row.changed_on)[:16] if row.changed_on else "",
				"summary": row.summary or "",
				"changes": {
					"added": raw.get("added") or [],
					"removed": raw.get("removed") or [],
					"modified": raw.get("modified") or [],
				},
			}
		)
	return items
def _sub_step_payload(sub_step) -> dict:
	return {
		"subStepKey": sub_step.sub_step_key,
		"label": sub_step.label or "",
		"description": sub_step.description or "",
		"sortOrder": int(sub_step.sort_order or 0),
		"evidenceRequired": bool(sub_step.evidence_required),
		"evidenceType": sub_step.evidence_type or "Note",
		"evidenceHint": sub_step.evidence_hint or "",
	}
def _step_payload(step, sub_steps: list) -> dict:
	return {
		"stepKey": step.step_key,
		"label": step.label or "",
		"description": step.description or "",
		"sortOrder": int(step.sort_order or 0),
		"requiresNote": bool(step.requires_note),
		"checklistStatus": step.checklist_status,
		"evidenceRequired": bool(step.evidence_required),
		"evidenceType": step.evidence_type or "Note",
		"evidenceHint": step.evidence_hint or "",
		"subSteps": [_sub_step_payload(sub_step) for sub_step in sub_steps],
	}
def _criterion_payload(criterion, graph=None) -> dict:
	by_criterion, sub_steps = graph if graph is not None else step_graph(criterion.parent)
	steps = by_criterion.get(criterion.criterion_id, [])
	return {
		"criterionId": criterion.criterion_id,
		"title": criterion.title,
		"statement": criterion.statement or "",
		"evidenceOptions": criterion.evidence_options or "",
		"isCritical": bool(criterion.is_critical),
		"steps": [
			_step_payload(step, sub_steps.get(step.name, [])) for step in steps
		],
	}
@frappe.whitelist()
@api_endpoint
def checklist() -> dict:
	"""Currently configured checklist version, its criteria (with step workflows and sub steps) and change history (read-only)."""
	require_platform()
	row = current_checklist()
	if not row:
		return APIResponse.success(data=checklist_payload(None, []))

	graph = step_graph(row.name)
	criteria = [
		_criterion_payload(criterion, graph)
		for criterion in criteria_of(row.name)
	]
	return APIResponse.success(
		data=checklist_payload(row, criteria)
	)
def _criterion_entry_from_row(row, graph=None) -> dict:
	"""Normalized form of a stored criterion (link-only child row plus its
	merged ``HISI Criterion`` text) and its steps."""
	by_criterion, sub_steps = graph if graph is not None else step_graph(row.parent)
	return {
		"title": row.title or "",
		"statement": row.statement or "",
		"evidence_options": row.evidence_options or "",
		"is_critical": int(row.is_critical or 0),
		"steps": [
			{
				"step_key": step.step_key,
				"label": step.label or "",
				"description": step.description or "",
				"sort_order": int(step.sort_order or 0),
				"checklist_status": step.checklist_status,
				"requires_note": int(step.requires_note or 0),
				"evidence_required": int(step.evidence_required or 0),
				"evidence_type": step.evidence_type or "Note",
				"evidence_hint": step.evidence_hint or "",
				"sub_steps": [
					{
						"sub_step_key": sub.sub_step_key,
						"label": sub.label or "",
						"description": sub.description or "",
						"sort_order": int(sub.sort_order or 0),
						"evidence_required": int(sub.evidence_required or 0),
						"evidence_type": sub.evidence_type or "Note",
						"evidence_hint": sub.evidence_hint or "",
					}
					for sub in sub_steps.get(step.name, [])
				],
			}
			for step in by_criterion.get(row.criterion_id, [])
		],
	}

def _criterion_signature(entry: dict) -> tuple:
	"""Stable comparable signature of one normalized criterion entry (incl. steps)."""
	steps = []
	for step in entry["steps"]:
		sub_steps = tuple(
			sorted(
				(
					sub["sub_step_key"],
					sub["label"],
					sub["description"],
					sub["sort_order"],
					sub["evidence_required"],
					sub["evidence_type"],
					sub["evidence_hint"],
				)
				for sub in step.get("sub_steps", [])
			)
		)
		steps.append(
			(
				step["step_key"],
				step["label"],
				step["description"],
				step["sort_order"],
				step["checklist_status"],
				step["requires_note"],
				step["evidence_required"],
				step["evidence_type"],
				step["evidence_hint"],
				sub_steps,
			)
		)
	steps.sort(key=lambda s: (s[3], s[0]))
	return (
		entry["title"],
		entry["statement"],
		entry["evidence_options"],
		entry["is_critical"],
		tuple(steps),
	)
def _enabled_status_keys() -> set[str]:
	return set(
		frappe.get_all("HISI Checklist Status", filters={"enabled": 1}, pluck="status_key")
	)
def _default_status_key(enabled: set[str] | None = None) -> str:
	enabled = enabled if enabled is not None else _enabled_status_keys()
	for preferred in ("pending", "in_progress"):
		if preferred in enabled:
			return preferred
	rows = frappe.get_all(
		"HISI Checklist Status",
		filters={"enabled": 1},
		fields=["status_key"],
		order_by="is_start desc, sort_order asc, status_key asc",
		limit_page_length=1,
	)
	return rows[0].status_key if rows else ""
def _normalize_evidence(criterion_id: str, subject: str, raw: dict) -> tuple[int, str, str]:
	required = 1 if raw.get("evidence_required") else 0
	evidence_type = (raw.get("evidence_type") or "Note").strip() or "Note"
	if evidence_type not in EVIDENCE_TYPES:
		frappe.throw(
			f"{subject} of criterion '{criterion_id}' has an unknown evidence type "
			f"'{evidence_type}' (use one of: {', '.join(EVIDENCE_TYPES)}).",
			frappe.ValidationError,
		)
	hint = (raw.get("evidence_hint") or "").strip()[:MAX_EVIDENCE_HINT_LENGTH]
	return required, evidence_type, hint
def _normalize_sub_steps(criterion_id: str, step_key: str, raw_sub_steps: list) -> list[dict]:
	if not raw_sub_steps:
		return []
	if len(raw_sub_steps) > MAX_SUB_STEP_COUNT:
		frappe.throw(
			f"Step '{step_key}' of criterion '{criterion_id}' has too many sub steps: "
			f"at most {MAX_SUB_STEP_COUNT} are supported.",
			frappe.ValidationError,
		)
	sub_steps: list[dict] = []
	seen: set[str] = set()
	for position, raw in enumerate(raw_sub_steps):
		subject = f"Sub step #{position + 1}"
		if not isinstance(raw, dict):
			frappe.throw(
				f"{subject} of step '{step_key}' (criterion '{criterion_id}') must be an object",
				frappe.ValidationError,
			)
		key = (raw.get("sub_step_key") or "").strip().lower()
		label = (raw.get("label") or "").strip()
		if not STATUS_SLUG_PATTERN.match(key):
			frappe.throw(
				f"{subject} of step '{step_key}' (criterion '{criterion_id}') has an invalid "
				"sub step key (use a lowercase slug, e.g. a1s1).",
				frappe.ValidationError,
			)
		if not label:
			frappe.throw(
				f"Sub step '{key}' of step '{step_key}' (criterion '{criterion_id}') needs a label.",
				frappe.ValidationError,
			)
		if key in seen:
			frappe.throw(
				f"Duplicate sub step key '{key}' in step '{step_key}' of criterion '{criterion_id}'.",
				frappe.ValidationError,
			)
		seen.add(key)
		required, evidence_type, hint = _normalize_evidence(
			criterion_id, f"sub step '{key}' of step '{step_key}'", raw
		)
		sub_steps.append(
			{
				"sub_step_key": key,
				"label": label[:MAX_STATUS_LABEL_LENGTH],
				"description": (raw.get("description") or "").strip()[:MAX_STATUS_DESCRIPTION_LENGTH],
				"sort_order": max(0, min(_as_int(raw.get("sort_order"), position + 1), MAX_SORT_ORDER)),
				"evidence_required": required,
				"evidence_type": evidence_type,
				"evidence_hint": hint,
			}
		)
	return sub_steps


def _normalize_criterion_steps(
	criterion_id: str, raw_steps: list, default_status: str, enabled_statuses: set[str]
) -> list[dict]:
	"""Validate one criterion's ordered step list and its nested sub steps."""
	steps: list[dict] = []
	if not raw_steps:
		return steps
	if len(raw_steps) > MAX_STEP_COUNT:
		frappe.throw(
			f"Criterion '{criterion_id}' has too many steps: at most {MAX_STEP_COUNT} are supported.",
			frappe.ValidationError,
		)
	seen: set[str] = set()
	for position, raw in enumerate(raw_steps):
		if not isinstance(raw, dict):
			frappe.throw(f"Each step of criterion '{criterion_id}' must be an object", frappe.ValidationError)
		key = (raw.get("step_key") or "").strip().lower()
		label = (raw.get("label") or "").strip()
		if not STATUS_SLUG_PATTERN.match(key):
			frappe.throw(
				f"Criterion '{criterion_id}' has a step with an invalid step key (use a lowercase slug, e.g. test_plan).",
				frappe.ValidationError,
			)
		if not label:
			frappe.throw(f"Step '{key or '?'}' of criterion '{criterion_id}' needs a label.", frappe.ValidationError)
		if key in seen:
			frappe.throw(f"Duplicate step key '{key}' in criterion '{criterion_id}'.", frappe.ValidationError)
		seen.add(key)
		status = (raw.get("checklist_status") or "").strip() or default_status
		if status not in enabled_statuses:
			frappe.throw(
				f"Step '{key}' of criterion '{criterion_id}' uses unknown or disabled checklist "
				f"status '{status}'.",
				frappe.ValidationError,
			)
		required, evidence_type, hint = _normalize_evidence(
			criterion_id, f"step '{key}'", raw
		)
		steps.append(
			{
				"step_key": key,
				"label": label[:MAX_STATUS_LABEL_LENGTH],
				"description": (raw.get("description") or "").strip()[:MAX_STATUS_DESCRIPTION_LENGTH],
				"sort_order": max(0, min(_as_int(raw.get("sort_order"), position + 1), MAX_SORT_ORDER)),
				"checklist_status": status,
				"requires_note": 1 if raw.get("requires_note", True) else 0,
				"evidence_required": required,
				"evidence_type": evidence_type,
				"evidence_hint": hint,
				"sub_steps": _normalize_sub_steps(
					criterion_id, key, _as_list(raw.get("sub_steps"))
				),
			}
		)
	return steps


def _normalize_criterion_entries(entries: list) -> list[dict]:
	enabled_statuses = _enabled_status_keys()
	if not enabled_statuses:
		frappe.throw(
			"No enabled checklist status is configured; add one before saving steps.",
			frappe.ValidationError,
		)
	default_status = _default_status_key(enabled_statuses)
	normalized: list[dict] = []
	seen: set[str] = set()
	for entry in entries:
		if not isinstance(entry, dict):
			frappe.throw("Each criterion must be an object", frappe.ValidationError)
		criterion_id = (entry.get("criterion_id") or "").strip()
		title = (entry.get("title") or "").strip()
		if not criterion_id or not title:
			frappe.throw("Each criterion needs criterion_id and title", frappe.ValidationError)
		if criterion_id in seen:
			frappe.throw(f"Duplicate criterion id: {criterion_id}", frappe.ValidationError)
		seen.add(criterion_id)
		normalized.append(
			{
				"criterion_id": criterion_id[:64],
				"title": title[:MAX_STATUS_LABEL_LENGTH],
				"statement": (entry.get("statement") or "").strip()[:2000],
				"evidence_options": (entry.get("evidence_options") or "").strip()[:1000],
				"is_critical": 1 if entry.get("is_critical") else 0,
				"steps": _normalize_criterion_steps(
					criterion_id, _as_list(entry.get("steps")), default_status, enabled_statuses
				),
			}
		)
	return normalized
def _upsert_criteria(entries: list[dict]) -> None:
	for entry in entries:
		values = {
			"title": entry["title"],
			"statement": entry["statement"],
			"evidence_options": entry["evidence_options"],
			"is_critical": entry["is_critical"],
		}
		existing = frappe.db.exists("HISI Criterion", entry["criterion_id"])
		if existing:
			criterion = frappe.get_doc("HISI Criterion", existing)
			if any(criterion.get(field) != value for field, value in values.items()):
				criterion.update(values)
				criterion.save(ignore_permissions=True)
		else:
			frappe.get_doc(
				{"doctype": "HISI Criterion", "criterion_id": entry["criterion_id"], **values}
			).insert(ignore_permissions=True)

@frappe.whitelist()
def save_checklist(criteria, release_notes: str = "") -> dict:
	user = require_platform()
	if isinstance(criteria, str):
		criteria = frappe.parse_json(criteria)
	if not isinstance(criteria, list) or not criteria:
		frappe.throw("criteria must be a non-empty list", frappe.ValidationError)

	normalized = _normalize_criterion_entries(criteria)

	row = current_checklist()
	graph = step_graph(row.name) if row else ({}, {})
	if row:
		doc = frappe.get_doc("HISI Checklist Version", row.name)
		old_entries = {
			criterion.criterion_id: _criterion_entry_from_row(criterion, graph)
			for criterion in criteria_of(row.name)
		}
		old_release_notes = doc.release_notes or ""
	else:
		doc = frappe.get_doc(
			{
				"doctype": "HISI Checklist Version",
				"version": "1.0",
				"effective_on": frappe.utils.today(),
				"published": 1,
				"enabled": 1,
				"release_notes": "Checklist created from the HISI administrator workspace.",
			}
		)
		old_entries = {}
		old_release_notes = ""

	new_map = {entry["criterion_id"]: entry for entry in normalized}
	old_signatures = {cid: _criterion_signature(entry) for cid, entry in old_entries.items()}
	added = [entry["criterion_id"] for entry in normalized if entry["criterion_id"] not in old_entries]
	removed = [cid for cid in old_entries if cid not in new_map]
	modified = [
		cid
		for cid in new_map
		if cid in old_signatures and _criterion_signature(new_map[cid]) != old_signatures[cid]
	]
	_upsert_criteria(normalized)
	doc.set("criteria", [])
	for entry in normalized:
		doc.append("criteria", {"criterion_id": entry["criterion_id"]})
	new_release_notes = (release_notes or "").strip()[:500]
	if new_release_notes:
		doc.release_notes = new_release_notes

	parts = []
	if added:
		parts.append("Added " + ", ".join(added))
	if removed:
		parts.append("Removed " + ", ".join(removed))
	if modified:
		parts.append("Updated " + ", ".join(modified))
	if new_release_notes and new_release_notes != old_release_notes:
		parts.append("Release notes updated")
	summary = "; ".join(parts) or "Checklist saved (no content changes)"
	doc.append(
		"changes",
		{
			"changed_by": user,
			"changed_on": frappe.utils.now(),
			"summary": summary,
			"changes": frappe.as_json({"added": added, "removed": removed, "modified": modified}),
		},
	)
	if doc.is_new():
		doc.insert()
	else:
		doc.save(ignore_permissions=True)

	_sync_criterion_steps(doc.name, normalized)

	frappe.db.commit()

	return checklist()


def _sync_criterion_steps(version_name: str, entries: list[dict]) -> None:
	"""Insert/update/delete the standalone step and sub step rows of a version.

	Steps are keyed by ``(criterion_id, step_key)``, sub steps by
	``(step name, sub_step_key)``. Only checklist configuration is touched:
	case-side assessments and progress rows are never deleted by a checklist
	edit, they simply stop counting once their criterion is gone.
	"""
	criterion_names = {row.criterion_id: row.name for row in criteria_of(version_name)}
	existing = {(step.criterion_id, step.step_key): step for step in steps_of(version_name)}
	kept: set[str] = set()

	for entry in entries:
		criterion_name = criterion_names.get(entry["criterion_id"])
		if not criterion_name:
			continue
		for step in entry["steps"]:
			doc = _upsert_step(version_name, entry["criterion_id"], step)
			kept.add(doc.name)
			_sync_sub_steps(doc.name, step["sub_steps"])

	stale = [step.name for key, step in existing.items() if step.name not in kept]
	if stale:
		frappe.db.delete("HISI Checklist Criterion Sub Step", {"step": ["in", stale]})
		frappe.db.delete("HISI Checklist Criterion Step", {"name": ["in", stale]})


def _upsert_step(version_name: str, criterion_id: str, step: dict):
	"""Insert or update one normalized step row (sub steps are synced by the caller)."""
	values = {
		"checklist_version": version_name,
		"criterion_id": criterion_id,
		"step_key": step["step_key"],
		"label": step["label"],
		"description": step["description"],
		"sort_order": step["sort_order"],
		"checklist_status": step["checklist_status"],
		"requires_note": step["requires_note"],
		"evidence_required": step["evidence_required"],
		"evidence_type": step["evidence_type"],
		"evidence_hint": step["evidence_hint"],
	}
	stored = frappe.db.get_value(
		"HISI Checklist Criterion Step",
		{
			"checklist_version": version_name,
			"criterion_id": criterion_id,
			"step_key": step["step_key"],
		},
		"name",
	)
	if stored:
		doc = frappe.get_doc("HISI Checklist Criterion Step", stored)
		if any(doc.get(field) != value for field, value in values.items()):
			doc.update(values)
			doc.save(ignore_permissions=True)
		return doc
	return frappe.get_doc({"doctype": "HISI Checklist Criterion Step", **values}).insert(
		ignore_permissions=True
	)


def _sync_sub_steps(step_name: str, sub_steps: list[dict]) -> None:
	stored = {
		row.sub_step_key: row
		for row in frappe.get_all(
			"HISI Checklist Criterion Sub Step", filters={"step": step_name}, fields=["*"]
		)
	}
	kept: set[str] = set()
	for sub_step in sub_steps:
		values = {"step": step_name, **{k: v for k, v in sub_step.items() if k != "sub_step_key"}}
		row = stored.get(sub_step["sub_step_key"])
		if row:
			doc = frappe.get_doc("HISI Checklist Criterion Sub Step", row.name)
			if any(doc.get(field) != value for field, value in values.items()):
				doc.update(values)
				doc.save(ignore_permissions=True)
		else:
			frappe.get_doc(
				{"doctype": "HISI Checklist Criterion Sub Step", "sub_step_key": sub_step["sub_step_key"], **values}
			).insert(ignore_permissions=True)
		kept.add(sub_step["sub_step_key"])

	stale = [row.name for key, row in stored.items() if key not in kept]
	if stale:
		frappe.db.delete("HISI Checklist Criterion Sub Step", {"name": ["in", stale]})


@frappe.whitelist()
def overview() -> dict:
	"""Platform overview for the administrator dashboard.

	Total vendor organizations plus a count per configured (enabled) payer
	validation status, and the number of organizations not judged yet.
	Platform-gated.
	"""
	require_platform()
	statuses = _status_map()
	total = frappe.db.count("HISI Vendor Organization")
	counts: dict[str, int] = {}
	without_status = 0
	for value, count in frappe.db.sql(
		"SELECT COALESCE(payer_validation_status, ''), COUNT(*)"
		" FROM `tabHISI Vendor Organization`"
		" GROUP BY payer_validation_status"
	):
		if value:
			counts[str(value)] = int(count)
		else:
			without_status = int(count)
	by_status = [
		{
			"statusKey": row.status_key,
			"label": row.label,
			"judgementType": row.judgement_type,
			"count": counts.get(row.name, 0),
		}
		for row in sorted(
			(row for row in statuses.values() if row.enabled),
			key=lambda r: (int(r.sort_order or 0), r.status_key),
		)
	]
	return APIResponse.success(data={
		"total": int(total),
		"byStatus": by_status,
		"withoutStatus": without_status,
	})


def _coerce_page(page, page_size) -> tuple[int, int]:
	"""Clamped 1-based page and page size for the vendor listing."""
	try:
		page_number = max(int(page), 1)
	except (TypeError, ValueError):
		page_number = 1
	try:
		size = int(page_size)
	except (TypeError, ValueError):
		size = DEFAULT_VENDOR_PAGE_SIZE
	return page_number, min(max(size, 1), MAX_VENDOR_PAGE_SIZE)


def _status_map() -> dict:
	"""Checklist status rows keyed by document name (== status_key)."""
	return {
		row.name: row
		for row in frappe.get_all(
			"HISI Checklist Status",
			fields=[
				"name",
				"status_key",
				"label",
				"judgement_type",
				"enabled",
				"is_start",
				"requires_comment",
				"sort_order",
				"description",
			],
		)
	}


def _transitions_by_status(status_names: list[str]) -> dict:
	"""Allowed next statuses grouped by parent status name."""
	grouped: dict[str, list] = {}
	if not status_names:
		return grouped
	for row in frappe.get_all(
		"HISI Checklist Status Transition",
		filters={
			"parenttype": "HISI Checklist Status",
			"parent": ["in", status_names],
		},
		fields=["parent", "to_status", "action_label", "idx"],
		order_by="idx asc",
	):
		grouped.setdefault(row.parent, []).append(
			{"statusKey": row.to_status, "actionLabel": row.action_label or ""}
		)
	return grouped


@frappe.whitelist()
def list_checklist_statuses() -> dict:
	"""All configured payer validation statuses with their workflow graph.

	Feeds the administrator Checklist page: which statuses exist, which are
	enabled, which one starts the workflow and which statuses may follow
	each one (empty list = terminal).
	"""
	require_platform()
	statuses = _status_map()
	grouped = _transitions_by_status(list(statuses))
	items = [
		{
			"statusKey": row.status_key,
			"label": row.label,
			"judgementType": row.judgement_type,
			"enabled": bool(row.enabled),
			"isStart": bool(row.is_start),
			"requiresComment": bool(row.requires_comment),
			"sortOrder": int(row.sort_order or 0),
			"description": row.description or "",
			"allowedNext": grouped.get(row.name, []),
		}
		for row in sorted(statuses.values(), key=lambda r: (int(r.sort_order or 0), r.status_key))
	]
	return {"statuses": items}


@frappe.whitelist()
def vendor_validation_status(
	page: int = 1, page_size: int = 20, status: str = "", search: str = ""
) -> dict:
	require_platform()
	page_number, size = _coerce_page(page, page_size)

	where = ""
	params: list = []
	status = (status or "").strip()
	search = (search or "").strip()[:MAX_VENDOR_FILTER_LENGTH]
	if status == "__none__":
		# Sentinel for the "No status" filter: not judged yet.
		where += " AND (o.payer_validation_status IS NULL OR o.payer_validation_status = '')"
	elif status:
		where += " AND o.payer_validation_status = %s"
		params.append(status)
	if search:
		where += " AND o.organization_name LIKE %s"
		params.append(f"%{search}%")

	base_from = (
		" FROM `tabHISI Vendor Organization` o"
		" LEFT JOIN `tabHISI Checklist Status` s ON s.name = o.payer_validation_status"
		f" WHERE 1=1{where}"
	)
	total = frappe.db.sql(f"SELECT COUNT(*){base_from}", tuple(params))[0][0]

	# Counts per status for the filter tabs, honouring the search term but
	# not the status filter itself (so every tab stays selectable).
	count_where = ""
	count_params: list = []
	if search:
		count_where = " AND o.organization_name LIKE %s"
		count_params.append(f"%{search}%")
	status_counts: dict[str, int] = {}
	without_status = 0
	for value, count in frappe.db.sql(
		(
			"SELECT COALESCE(o.payer_validation_status, ''), COUNT(*)"
			" FROM `tabHISI Vendor Organization` o"
			f" WHERE 1=1{count_where}"
			" GROUP BY o.payer_validation_status"
		),
		tuple(count_params),
	):
		if value:
			status_counts[str(value)] = int(count)
		else:
			without_status = int(count)

	rows = frappe.db.sql(
		(
			"SELECT o.name AS org, o.organization_name, o.website, o.dha_certificate_id,"
			" o.payer_validation_status, o.payer_validation_note, o.modified,"
			" s.status_key, s.label AS status_label, s.judgement_type, s.requires_comment"
			f"{base_from}"
			" ORDER BY COALESCE(s.sort_order, 99999) ASC, o.organization_name ASC"
			" LIMIT %s OFFSET %s"
		),
		(*params, size, (page_number - 1) * size),
		as_dict=True,
	)

	statuses = _status_map()
	grouped = _transitions_by_status(
		[row.payer_validation_status for row in rows if row.payer_validation_status]
	)
	items = [
		{
			"organizationId": row.org,
			"organizationName": row.organization_name,
			"website": row.website or "",
			"dhaCertificateId": row.dha_certificate_id or "",
			"payerValidationStatus": row.status_key or "",
			"statusLabel": row.status_label or "",
			"judgementType": row.judgement_type or "",
			"requiresComment": bool(row.requires_comment),
			"note": row.payer_validation_note or "",
			# Moves the dashboard may offer for this vendor right now.
			"allowedNext": grouped.get(row.payer_validation_status, [])
			if row.payer_validation_status
			else [
				{"statusKey": s.status_key, "actionLabel": ""}
				for s in sorted(
					(statuses[k] for k in statuses if statuses[k].is_start and statuses[k].enabled),
					key=lambda r: (int(r.sort_order or 0), r.status_key),
				)
			],
			"modified": str(row.modified)[:16] if row.modified else "",
		}
		for row in rows
	]
	return APIResponse.success(data={
		"vendors": items,
		"page": page_number,
		"pageSize": size,
		"total": total,
		"totalPages": (total + size - 1) // size if total else 0,
		"statusCounts": status_counts,
		"withoutStatus": without_status,
	})


def _latest_case_for_org(organization_id: str) -> str | None:
	"""The organization's most recently modified validation case, if any."""
	return frappe.db.get_value(
		"HISI SHA Validation Case",
		{"vendor": organization_id, "docstatus": ["<", 2]},
		"name",
		order_by="modified desc",
	)


def _org_status_completion(organization_id: str) -> dict:
	"""Per-status step completion for the organization's latest case (``{}`` without one)."""
	case_name = _latest_case_for_org(organization_id)
	if not case_name:
		return {}
	return status_completion(case_name)


def _status_counts(completion: dict, status_key: str) -> tuple[bool, int]:
	"""``(all steps complete, how many are still open)`` for one status."""
	bucket = completion.get(status_key)
	if not bucket:
		return True, 0
	return bool(bucket["complete"]), len(bucket["remaining"])


def _open_status_steps(completion: dict, status_key: str) -> list:
	"""Open step labels of one checklist status (empty when complete)."""
	bucket = completion.get(status_key)
	if not bucket or bucket["complete"]:
		return []
	return bucket["remaining"]


@frappe.whitelist()
def set_vendor_validation_status(organization_id: str, status_key: str, comment: str = "") -> dict:
	user = require_platform()
	organization_id = (organization_id or "").strip()
	status_key = (status_key or "").strip()
	comment = (comment or "").strip()[:MAX_NOTE_LENGTH]
	if not organization_id:
		frappe.throw("organization_id is required", frappe.ValidationError)
	if not status_key:
		frappe.throw("status_key is required", frappe.ValidationError)
	if not frappe.db.exists("HISI Vendor Organization", organization_id):
		frappe.throw("Vendor organization not found", frappe.DoesNotExistError)

	statuses = _status_map()
	target = statuses.get(status_key)
	if not target or not target.enabled:
		frappe.throw(
			f"Status '{status_key}' is not an enabled checklist status.",
			frappe.ValidationError,
		)
	if target.requires_comment and not comment:
		frappe.throw(
			f"A comment is required for status '{target.label}'.",
			frappe.ValidationError,
		)

	current = frappe.db.get_value(
		"HISI Vendor Organization", organization_id, "payer_validation_status"
	)
	if current and current != status_key:
		allowed = frappe.db.exists(
			"HISI Checklist Status Transition",
			{"parenttype": "HISI Checklist Status", "parent": current, "to_status": status_key},
		)
		if not allowed:
			from_label = statuses[current].label if current in statuses else current
			frappe.throw(
				f"'{target.label}' is not an allowed next status from '{from_label}'.",
				frappe.ValidationError,
			)
	if not current and not target.is_start:
		frappe.throw(
			f"'{target.label}' is not configured as a start status.",
			frappe.ValidationError,
		)

	# Moving a case into a status (forwards or backwards) requires every
	# checklist step of that status to be complete first. Statuses without
	# steps pass automatically; an organization with no case is not gated.
	if current and current != status_key:
		open_steps = _open_status_steps(_org_status_completion(organization_id), status_key)
		if open_steps:
			shown = ", ".join(open_steps[:5])
			suffix = ", …" if len(open_steps) > 5 else ""
			frappe.throw(
				f"'{target.label}' cannot be set yet: {len(open_steps)} checklist step(s) "
				f"are still open ({shown}{suffix}).",
				frappe.ValidationError,
			)

	doc = frappe.get_doc("HISI Vendor Organization", organization_id)
	doc.payer_validation_status = status_key
	doc.payer_validation_note = comment
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"organizationId": organization_id,
		"payerValidationStatus": status_key,
		"statusLabel": target.label,
		"judgementType": target.judgement_type,
		"note": comment,
		"changedBy": user,
		"changedOn": str(frappe.utils.now())[:16],
	}


def _as_list(value) -> list:
	"""Accept a Python list or a JSON string (route-encoded bodies)."""
	if isinstance(value, str):
		value = frappe.parse_json(value)
	return value if isinstance(value, list) else []


def _as_int(value, default: int) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return default


def _normalize_status_entries(statuses) -> list[dict]:
	raw_entries = _as_list(statuses)
	if not raw_entries:
		frappe.throw("statuses must be a non-empty list", frappe.ValidationError)
	if len(raw_entries) > MAX_STATUS_COUNT:
		frappe.throw(
			f"Too many statuses: at most {MAX_STATUS_COUNT} are supported.",
			frappe.ValidationError,
		)

	normalized: list[dict] = []
	seen: set[str] = set()
	for entry in raw_entries:
		if not isinstance(entry, dict):
			frappe.throw("Each status must be an object", frappe.ValidationError)
		key = (entry.get("status_key") or "").strip().lower()
		label = (entry.get("label") or "").strip()
		if not STATUS_SLUG_PATTERN.match(key):
			frappe.throw(
				"Status Key must be a lowercase slug, e.g. approved, rejected, more_info_required.",
				frappe.ValidationError,
			)
		if not label:
			frappe.throw(f"Status '{key}' needs a label.", frappe.ValidationError)
		if key in seen:
			frappe.throw(f"Duplicate status key: {key}", frappe.ValidationError)
		seen.add(key)
		judgement = (entry.get("judgement_type") or "Other").strip()
		if judgement not in STATUS_JUDGEMENT_TYPES:
			frappe.throw(
				f"Unknown judgement type '{judgement}' for status '{key}'.",
				frappe.ValidationError,
			)
		enabled = 1 if entry.get("enabled") else 0
		normalized.append(
			{
				"status_key": key,
				"label": label[:MAX_STATUS_LABEL_LENGTH],
				"judgement_type": judgement,
				"sort_order": max(0, min(_as_int(entry.get("sort_order"), 0), MAX_SORT_ORDER)),
				"enabled": enabled,
				# A disabled status can never start the workflow.
				"is_start": 1 if enabled and entry.get("is_start") else 0,
				"requires_comment": 1 if entry.get("requires_comment") else 0,
				"description": (entry.get("description") or "").strip()[
					:MAX_STATUS_DESCRIPTION_LENGTH
				],
				"allowed_next": [],
			}
		)

	if not any(item["enabled"] and item["is_start"] for item in normalized):
		frappe.throw(
			"At least one status must be enabled and marked as the start status.",
			frappe.ValidationError,
		)
	enabled_keys = {item["status_key"] for item in normalized if item["enabled"]}

	for raw, item in zip(raw_entries, normalized):
		parsed: list[dict] = []
		targets: set[str] = set()
		for edge in _as_list(raw.get("allowed_next")):
			if not isinstance(edge, dict):
				frappe.throw("Each allowed next status must be an object", frappe.ValidationError)
			target = (edge.get("to_status") or "").strip()
			if not target:
				frappe.throw(
					f"Status '{item['status_key']}' has an allowed next status without a target.",
					frappe.ValidationError,
				)
			if target == item["status_key"]:
				frappe.throw(
					f"Status '{item['status_key']}' cannot transition to itself.",
					frappe.ValidationError,
				)
			if target in targets:
				frappe.throw(
					f"Status '{target}' is listed more than once for '{item['status_key']}'.",
					frappe.ValidationError,
				)
			targets.add(target)
			if target not in seen:
				frappe.throw(
					f"Allowed next status '{target}' is not part of this save.",
					frappe.ValidationError,
				)
			if target not in enabled_keys:
				frappe.throw(
					f"Allowed next status '{target}' must be enabled.",
					frappe.ValidationError,
				)
			parsed.append(
				{
					"to_status": target,
					"action_label": (edge.get("action_label") or "").strip()[
						:MAX_ACTION_LABEL_LENGTH
					],
				}
			)
		item["allowed_next"] = parsed
	return normalized


def _status_write_order(entries: list[dict]) -> list[dict]:
	return sorted(
		entries,
		key=lambda item: (
			0 if item["enabled"] and item["is_start"] else 1 if item["enabled"] else 2,
			item["sort_order"],
			item["status_key"],
		),
	)


@frappe.whitelist()
def save_checklist_statuses(statuses) -> dict:
	"""Upsert the administrator's configured payer validation statuses.

	``statuses`` is the full list of status objects (status_key, label,
	judgement_type, sort_order, enabled, is_start, requires_comment,
	description, allowed_next). The payload is validated as a whole —
	always at least one enabled start status, no self loops, no duplicate
	edges, every edge target enabled in this same save — then written in
	an order that keeps each save valid: base fields first with the
	workflow graph cleared, transitions second once every target is in
	its final state. Statuses absent from the payload are left alone;
	retire with ``enabled = 0`` rather than deleting. Platform-gated.
	Returns the fresh list_checklist_statuses payload.
	"""
	require_platform()
	entries = _normalize_status_entries(statuses)

	frappe.db.savepoint("save_checklist_statuses")
	try:
		for entry in _status_write_order(entries):
			existing = frappe.db.exists("HISI Checklist Status", entry["status_key"])
			if existing:
				doc = frappe.get_doc("HISI Checklist Status", existing)
			else:
				doc = frappe.get_doc(
					{"doctype": "HISI Checklist Status", "status_key": entry["status_key"]}
				)
			doc.label = entry["label"]
			doc.judgement_type = entry["judgement_type"]
			doc.sort_order = entry["sort_order"]
			doc.enabled = entry["enabled"]
			doc.is_start = entry["is_start"]
			doc.requires_comment = entry["requires_comment"]
			doc.description = entry["description"]
			# Edges are applied in the second pass, once every target row
			# holds its final enabled/disabled state.
			doc.set("allowed_next", [])
			if existing:
				doc.save(ignore_permissions=True)
			else:
				doc.insert(ignore_permissions=True)

		for entry in entries:
			if not entry["allowed_next"]:
				continue
			doc = frappe.get_doc("HISI Checklist Status", entry["status_key"])
			for edge in entry["allowed_next"]:
				doc.append(
					"allowed_next",
					{"to_status": edge["to_status"], "action_label": edge["action_label"]},
				)
			doc.save(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point="save_checklist_statuses")
		raise

	frappe.db.commit()
	return list_checklist_statuses()
@frappe.whitelist()
def get_vendor_validation_status(organization_id: str) -> dict:
	require_platform()
	organization_id = (organization_id or "").strip()
	if not organization_id:
		frappe.throw("organization_id is required", frappe.ValidationError)
	if not frappe.db.exists("HISI Vendor Organization", organization_id):
		frappe.throw("Vendor organization not found", frappe.DoesNotExistError)

	doc = frappe.db.get_value(
		"HISI Vendor Organization",
		organization_id,
		[
			"organization_name",
			"website",
			"dha_certificate_id",
			"payer_validation_status",
			"payer_validation_note",
			"modified",
		],
		as_dict=True,
	)
	statuses = _status_map()
	current = (doc.payer_validation_status or "").strip()
	current_row = statuses.get(current)
	# Step completion behind each offered move (empty when the org has no case).
	completion = _org_status_completion(organization_id)

	allowed = []
	if current:
		for row in frappe.get_all(
			"HISI Checklist Status Transition",
			filters={"parenttype": "HISI Checklist Status", "parent": current},
			fields=["to_status", "action_label"],
			order_by="idx asc",
		):
			target = statuses.get(row.to_status)
			steps_complete, steps_remaining = _status_counts(completion, row.to_status)
			allowed.append(
				{
					"statusKey": row.to_status,
					"label": target.label if target else row.to_status,
					"actionLabel": row.action_label or "",
					"requiresComment": bool(target.requires_comment) if target else False,
					"stepsComplete": steps_complete,
					"stepsRemaining": steps_remaining,
				}
			)
	else:
		# Not judged yet: any enabled start status may begin the workflow.
		for row in sorted(
			(statuses[k] for k in statuses if statuses[k].is_start and statuses[k].enabled),
			key=lambda r: (int(r.sort_order or 0), r.status_key),
		):
			steps_complete, steps_remaining = _status_counts(completion, row.status_key)
			allowed.append(
				{
					"statusKey": row.status_key,
					"label": row.label,
					"actionLabel": "",
					"requiresComment": bool(row.requires_comment),
					"stepsComplete": steps_complete,
					"stepsRemaining": steps_remaining,
				}
			)

	return {
		"organization": {
			"organizationId": organization_id,
			"organizationName": doc.organization_name,
			"website": doc.website or "",
			"dhaCertificateId": doc.dha_certificate_id or "",
			"modified": str(doc.modified)[:16] if doc.modified else "",
		},
		"status": {
			"statusKey": current,
			"label": current_row.label if current_row else "",
			"judgementType": current_row.judgement_type if current_row else "",
			"requiresComment": bool(current_row.requires_comment) if current_row else False,
			"isStart": bool(current_row.is_start) if current_row else False,
			"description": (current_row.description or "") if current_row else "",
			"note": doc.payer_validation_note or "",
		},
		"allowedNext": allowed,
	}

def _latest_enabled_checklist():
	rows = frappe.get_all(
		"HISI Checklist Version",
		filters={"published": 1, "enabled": 1},
		fields=["name", "version", "effective_on", "published", "enabled", "release_notes"],
		order_by="effective_on desc, modified desc",
		limit_page_length=1,
	)
	return rows[0] if rows else None

def _criterion_definition_payload(doc) -> dict:
	return {
		"criterionId": doc.criterion_id,
		"title": doc.title or "",
		"statement": doc.statement or "",
		"evidenceOptions": doc.evidence_options or "",
		"isCritical": bool(doc.is_critical),
	}

def _as_flag(value) -> int:
	"""A request flag (bool, 1/0 or 'true'/'false') as 0 or 1."""
	if isinstance(value, str):
		return 1 if value.strip().lower() in ("1", "true", "yes", "on") else 0
	return 1 if value else 0

@frappe.whitelist()
@api_endpoint
def fetch_latest_checklist() -> dict:
	require_platform()
	row = _latest_enabled_checklist()
	if not row:
		return APIResponse.success(data=checklist_payload(None, []))
	graph = step_graph(row.name)
	criteria = [
		_criterion_payload(criterion, graph) for criterion in criteria_of(row.name)
	]
	return APIResponse.success(data=checklist_payload(row, criteria))

@frappe.whitelist()
@api_endpoint
def update_criterion(
	criterion_id: str = "",
	title: str | None = None,
	statement: str | None = None,
	is_critical=None,
	evidence_options: str | None = None,
) -> dict:
	user = require_platform()
	key = (criterion_id or "").strip()
	if not key:
		raise BadRequest("criterion_id is required.")
	existing = frappe.db.exists("HISI Criterion", key)
	if not existing:
		raise NotFound(f"Criterion {key} does not exist.")

	updates = {}
	if title is not None:
		value = (title or "").strip()
		if not value:
			raise BadRequest("Title cannot be empty.")
		if len(value) > MAX_CRITERION_TITLE_LENGTH:
			raise BadRequest(f"Title is longer than {MAX_CRITERION_TITLE_LENGTH} characters.")
		updates["title"] = value
	if statement is not None:
		value = (statement or "").strip()
		if len(value) > MAX_CRITERION_STATEMENT_LENGTH:
			raise BadRequest(
				f"Statement is longer than {MAX_CRITERION_STATEMENT_LENGTH} characters."
			)
		updates["statement"] = value
	if evidence_options is not None:
		value = (evidence_options or "").strip()
		if len(value) > MAX_CRITERION_EVIDENCE_OPTIONS_LENGTH:
			raise BadRequest(
				"Evidence options are longer than "
				f"{MAX_CRITERION_EVIDENCE_OPTIONS_LENGTH} characters."
			)
		updates["evidence_options"] = value
	if is_critical is not None:
		updates["is_critical"] = _as_flag(is_critical)
	if not updates:
		raise BadRequest(
			"Provide at least one of title, statement, is_critical or evidence_options."
		)

	doc = frappe.get_doc("HISI Criterion", existing)
	changed = [field for field, value in updates.items() if doc.get(field) != value]
	if changed:
		labels = {
			"title": "title",
			"statement": "statement",
			"is_critical": "critical",
			"evidence_options": "evidence options",
		}
		doc.update({field: updates[field] for field in changed})
		doc.save(ignore_permissions=True)
		_log_checklist_change(
			user,
			f"Criterion {key} updated: "
			+ ", ".join(labels.get(field, field) for field in changed),
			[key],
		)
		frappe.db.commit()
	return APIResponse.success(
		data={**_criterion_definition_payload(doc), "updated": bool(changed)}
	)
def _editable_checklist():
	"""The checklist the administrator edits — published and enabled."""
	row = _latest_enabled_checklist()
	if not row:
		raise BadRequest("No published and enabled checklist is available.")
	return row
def _criterion_entry(row, criterion_id: str):
	"""That checklist's criterion row, merged with the predefined criterion."""
	entry = next(
		(criterion for criterion in criteria_of(row.name) if criterion.criterion_id == criterion_id),
		None,
	)
	if entry is None:
		raise NotFound(f"Criterion {criterion_id} is not part of checklist {row.version}.")
	return entry

@frappe.whitelist()
@api_endpoint
def fetch_criterion(criterion_id: str = "") -> dict:
	require_platform()
	key = (criterion_id or "").strip()
	if not key:
		raise BadRequest("criterion_id is required.")
	row = _editable_checklist()
	entry = _criterion_entry(row, key)
	return APIResponse.success(
		data={**_criterion_payload(entry, step_graph(row.name)), "checklistVersion": row.version}
	)
@frappe.whitelist()
@api_endpoint
def save_criterion_step(criterion_id: str = "", step=None) -> dict:
	"""Create or update one step of a criterion on the latest enabled checklist.

	``step`` is the per-step object ``save_checklist`` accepts: ``step_key``
	identifies it (a new key creates the step, an existing key edits it — the
	key itself is never rewritten, delete and re-create to rename), the
	remaining fields are editable, and ``sub_steps`` is the complete list for
	that step — a sub step missing from it is removed. Steps belong to a
	checklist version, so the edit lands on the published and enabled one.
	Platform-gated; returns the stored step payload.
	"""
	user = require_platform()
	key = (criterion_id or "").strip()
	if not key:
		raise BadRequest("criterion_id is required.")
	if isinstance(step, str):
		try:
			step = frappe.parse_json(step)
		except ValueError:
			step = None
	if not isinstance(step, dict):
		raise BadRequest("step must be an object.")

	row = _editable_checklist()
	_criterion_entry(row, key)

	enabled = _enabled_status_keys()
	if not enabled:
		raise BadRequest(
			"No enabled checklist status is configured; add one before saving steps."
		)
	normalized = _normalize_criterion_steps(key, [step], _default_status_key(enabled), enabled)[0]

	existing = frappe.db.get_value(
		"HISI Checklist Criterion Step",
		{
			"checklist_version": row.name,
			"criterion_id": key,
			"step_key": normalized["step_key"],
		},
		"name",
	)
	if not existing and (
		frappe.db.count(
			"HISI Checklist Criterion Step",
			filters={"checklist_version": row.name, "criterion_id": key},
		)
		>= MAX_STEP_COUNT
	):
		raise BadRequest(
			f"Criterion {key} already has the maximum of {MAX_STEP_COUNT} steps."
		)

	doc = _upsert_step(row.name, key, normalized)
	_sync_sub_steps(doc.name, normalized["sub_steps"])
	_log_checklist_change(
		user, f"Criterion {key} step {normalized['step_key']} saved", [key]
	)
	frappe.db.commit()

	_, sub_steps = step_graph(row.name)
	return APIResponse.success(data=_step_payload(doc, sub_steps.get(doc.name, [])))


@frappe.whitelist()
@api_endpoint
def delete_criterion_step(criterion_id: str = "", step_key: str = "") -> dict:
	"""Remove one step and its sub steps from the latest enabled checklist.

	Case-side rows (assessments, step progress) are kept: they simply stop
	counting, exactly as when a checklist is edited through
	``save_checklist``. Platform-gated.
	"""
	user = require_platform()
	key = (criterion_id or "").strip()
	slug = (step_key or "").strip().lower()
	if not key or not slug:
		raise BadRequest("criterion_id and step_key are required.")
	row = _editable_checklist()
	_criterion_entry(row, key)

	name = frappe.db.get_value(
		"HISI Checklist Criterion Step",
		{"checklist_version": row.name, "criterion_id": key, "step_key": slug},
		"name",
	)
	if not name:
		raise NotFound(f"Step {slug} does not exist on criterion {key}.")

	frappe.db.delete("HISI Checklist Criterion Sub Step", {"step": name})
	frappe.db.delete("HISI Checklist Criterion Step", {"name": name})
	_log_checklist_change(user, f"Criterion {key} step {slug} deleted", [key])
	frappe.db.commit()

	return APIResponse.success(
		data={"criterionId": key, "stepKey": slug, "deleted": True}
	)

def _log_checklist_change(user: str, summary: str, criterion_ids: list[str]) -> None:
	"""Record an administrator edit in the checklist change history."""
	row = current_checklist()
	if not row:
		return
	doc = frappe.get_doc("HISI Checklist Version", row.name)
	doc.append(
		"changes",
		{
			"changed_by": user,
			"changed_on": frappe.utils.now(),
			"summary": summary,
			"changes": frappe.as_json(
				{"added": [], "removed": [], "modified": criterion_ids}
			),
		},
	)
	doc.save(ignore_permissions=True)

@frappe.whitelist()
@api_endpoint
def update_admin_profile(full_name: str = "", phone_number: str = "", gender: str = "") -> dict:
	user = require_platform()
	name = (full_name or "").strip()
	phone = (phone_number or "").strip()
	selected_gender = (gender or "").strip()

	if not name and not phone and not selected_gender:
		raise BadRequest("Provide at least one of full_name, phone_number or gender.")
	if name and len(name) > MAX_DISPLAY_NAME_LENGTH:
		raise BadRequest("Display name is too long.")
	if phone and not PHONE_PATTERN.match(phone):
		raise BadRequest(
			"Phone number must be 5-20 characters: digits, spaces, dashes, "
			"parentheses, optionally with a leading +."
		)
	if selected_gender and selected_gender not in GENDER_OPTIONS:
		raise BadRequest("Gender must be either 'Male' or 'Female'.")

	doc = frappe.get_doc("User", user)
	changed = False
	if name and doc.full_name != name:
		# User.full_name is read-only: Frappe recomputes it on save from the
		# first/middle/last parts, so write those instead of full_name.
		parts = name.split()
		doc.first_name = parts[0]
		doc.middle_name = " ".join(parts[1:-1]) if len(parts) > 2 else ""
		doc.last_name = parts[-1] if len(parts) > 1 else ""
		changed = True
	if phone and doc.mobile_no != phone:
		# Frappe resolves phone-based logins against User.mobile_no, not the
		# display-only User.phone field, so the login-capable one is written.
		doc.mobile_no = phone
		changed = True
	if selected_gender and doc.gender != selected_gender:
		doc.gender = selected_gender
		changed = True
	if changed:
		doc.save(ignore_permissions=True)
		frappe.db.commit()
	return APIResponse.success(data={
		"email": user,
		"fullName": doc.full_name or "",
		"phoneNumber": doc.mobile_no or "",
		"gender": doc.gender or "",
		"updated": changed,
	})