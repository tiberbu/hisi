"""Administrator oversight of SHA validation cases.

Backs the HISI Administrator workspace: list every organization's validation
case with a per-criterion roll-up, and let an administrator acknowledge that
a case passed technical validation. Both methods require the ``System
Manager`` role here; the frontend route guard only controls presentation.
"""

import frappe
from frappe.model.workflow import WorkflowTransitionError, apply_workflow

# The only role that defines the administrator separation.
ADMIN_ROLE = "System Manager"

# Assessments that count towards a criterion being cleared.
PASS_ASSESSMENTS = ("Pass", "Pass with observation")

MAX_LISTED_CASES = 200
MAX_NOTE_LENGTH = 280


def _require_platform() -> str:
	"""Return the session user, or throw when not a System Manager."""
	user = frappe.session.user
	if user == "Guest" or ADMIN_ROLE not in frappe.get_roles(user):
		frappe.throw("Not authorized", frappe.PermissionError)
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


def _display_maps() -> tuple[dict, dict, dict]:
	"""Vendor display names, product names and certificate IDs by document name."""
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
	"""All SHA validation cases with vendor/product labels and criterion roll-ups."""
	_require_platform()

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
		filters={
			"parenttype": "HISI SHA Validation Case",
			"parent": ["in", [case.name for case in cases]],
		},
		fields=["parent", "assessment"],
	):
		assessments.setdefault(row.parent, []).append(row)

	items = []
	for case in cases:
		steps = _rollup(assessments.get(case.name, []))
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
				# Ready when every criterion is assessed and none failed.
				"canAcknowledge": bool(steps["total"]) and not steps["pending"] and not steps["failed"]
				and case.state != "Validation passed",
				"acknowledgedBy": case.acknowledged_by or "",
				"acknowledgedOn": str(case.acknowledged_on)[:16] if case.acknowledged_on else "",
				"acknowledgementNote": case.acknowledgement_note or "",
				"modified": str(case.modified)[:16] if case.modified else "",
			}
		)
	return {"cases": items}


@frappe.whitelist()
def acknowledge_validation(case_id: str, note: str = "") -> dict:
	"""Record platform acknowledgement that a fully passed case is accepted.

	Only cases with at least one criterion and no pending or failed
	assessments may be acknowledged; the state moves to "Validation passed"
	with an audit trail (who, when, note).
	"""
	user = _require_platform()
	if not case_id:
		frappe.throw("case_id is required", frappe.ValidationError)

	case = frappe.get_doc("HISI SHA Validation Case", case_id)
	steps = _rollup(case.get("criteria_assessments") or [])
	if not steps["total"] or steps["pending"] or steps["failed"]:
		frappe.throw("Case is not ready for acknowledgement", frappe.ValidationError)

	# The engine validates the transition (role + self-approval) and records
	# the move to Validation passed with an audit comment.
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


def _current_checklist():
	"""The published checklist version, else the most recently modified one."""
	fields = ["name", "version", "effective_on", "published", "release_notes"]
	published = frappe.get_all(
		"HISI Checklist Version",
		filters={"published": 1},
		fields=fields,
		order_by="effective_on desc, modified desc",
		limit_page_length=1,
	)
	if published:
		return published[0]
	latest = frappe.get_all(
		"HISI Checklist Version", fields=fields, order_by="modified desc", limit_page_length=1
	)
	return latest[0] if latest else None


def _checklist_payload(row, criteria) -> dict:
	return {
		"version": row.version if row else "",
		"effectiveOn": (row.effective_on if row else None) or "",
		"published": int(row.published or 0) if row else 0,
		"releaseNotes": (row.release_notes if row else None) or "",
		"criteria": criteria,
	}


def _criterion_payload(criterion) -> dict:
	return {
		"criterionId": criterion.criterion_id,
		"title": criterion.title,
		"statement": criterion.statement or "",
		"evidenceOptions": criterion.evidence_options or "",
		"isCritical": bool(criterion.is_critical),
	}


@frappe.whitelist()
def get_checklist() -> dict:
	"""Currently configured checklist version and its criteria (read-only)."""
	_require_platform()
	row = _current_checklist()
	if not row:
		return _checklist_payload(None, [])

	criteria = frappe.get_all(
		"HISI Checklist Criterion",
		filters={"parenttype": "HISI Checklist Version", "parent": row.name},
		fields=["criterion_id", "title", "statement", "evidence_options", "is_critical"],
		order_by="idx asc",
	)
	return _checklist_payload(row, [_criterion_payload(c) for c in criteria])


def _next_version(current: str) -> str:
	"""Minor-bump a dotted version string (1.0 -> 1.1); sensible fallbacks."""
	parts = (current or "").split(".")
	if len(parts) == 2 and all(part.isdigit() for part in parts):
		return f"{int(parts[0])}.{int(parts[1]) + 1}"
	return f"{current}.1" if current else "1.0"


@frappe.whitelist()
def save_checklist(criteria, release_notes: str = "") -> dict:
	"""Save checklist modifications (HISI-FR-011 aware).

	Platform-gated (System Manager). Published releases are immutable, so
	saving either edits the current *draft* version in place or — when the
	current version is published — creates the next version with the
	submitted criteria (minor bump), which becomes the configured checklist.
	Cases keep their original checklist_version link untouched.
	"""
	_require_platform()
	if isinstance(criteria, str):
		criteria = frappe.parse_json(criteria)
	if not isinstance(criteria, list) or not criteria:
		frappe.throw("criteria must be a non-empty list", frappe.ValidationError)

	normalized = []
	seen: set[str] = set()
	for entry in criteria:
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
				"title": title[:140],
				"statement": (entry.get("statement") or "").strip()[:2000],
				"evidence_options": (entry.get("evidence_options") or "").strip()[:1000],
				"is_critical": 1 if entry.get("is_critical") else 0,
			}
		)

	row = _current_checklist()
	created_new_version = not row or bool(row.published)

	if created_new_version:
		doc = frappe.get_doc(
			{
				"doctype": "HISI Checklist Version",
				"version": _next_version(row.version if row else ""),
				"effective_on": frappe.utils.today(),
				"published": 1,
				"release_notes": (release_notes or "").strip()[:500]
				or ("Checklist revisions saved from the HISI administrator workspace."),
			}
		)
	else:
		doc = frappe.get_doc("HISI Checklist Version", row.name)
		if release_notes:
			doc.release_notes = release_notes.strip()[:500]

	doc.set("criteria", [])
	for entry in normalized:
		doc.append("criteria", entry)
	doc.insert() if created_new_version else doc.save()
	frappe.db.commit()

	criteria_rows = frappe.get_all(
		"HISI Checklist Criterion",
		filters={"parenttype": "HISI Checklist Version", "parent": doc.name},
		fields=["criterion_id", "title", "statement", "evidence_options", "is_critical"],
		order_by="idx asc",
	)
	payload = _checklist_payload(doc, [_criterion_payload(c) for c in criteria_rows])
	payload["createdNewVersion"] = created_new_version
	return payload
