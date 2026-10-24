import re

import frappe
from frappe.model.workflow import WorkflowTransitionError, apply_workflow

ADMIN_ROLE = "System Manager"
PASS_ASSESSMENTS = ("Pass", "Pass with observation")
STATUS_JUDGEMENT_TYPES = ("Approve", "Reject", "Request More Info", "Other")
STATUS_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

MAX_LISTED_CASES = 200
MAX_NOTE_LENGTH = 280
DEFAULT_VENDOR_PAGE_SIZE = 20
MAX_VENDOR_PAGE_SIZE = 100
MAX_VENDOR_FILTER_LENGTH = 64
MAX_STATUS_COUNT = 50
MAX_STATUS_LABEL_LENGTH = 140
MAX_STATUS_DESCRIPTION_LENGTH = 500
MAX_ACTION_LABEL_LENGTH = 64
MAX_SORT_ORDER = 9999

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
	_require_platform()
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
def list_vendor_validation_status(
	page: int = 1, page_size: int = 20, status: str = "", search: str = ""
) -> dict:
	"""Paginated vendor organizations with their payer validation status.

	Sorted by the status's configured sort order (statuses needing attention
	first when configured that way), then organization name; organizations
	without a status sort last. Optionally filter by a single status_key or
	search by organization name. Platform-gated; the Frappe route already
	limits callers to a session, and this method enforces the admin role.
	"""
	_require_platform()
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
	return {
		"vendors": items,
		"page": page_number,
		"pageSize": size,
		"total": total,
		"totalPages": (total + size - 1) // size if total else 0,
		"statusCounts": status_counts,
		"withoutStatus": without_status,
	}


@frappe.whitelist()
def set_vendor_validation_status(organization_id: str, status_key: str, comment: str = "") -> dict:
	"""Move a vendor organization's payer validation status along the workflow.

	The target must be an enabled HISI Checklist Status and must be an
	allowed *next* status of the organization's current status (or a start
	status when it has none). Statuses flagged requires_comment reject empty
	comments. Platform-gated.
	"""
	user = _require_platform()
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
	"""Validate a full save payload for the checklist status workflow.

	The workflow graph is checked against the payload as a whole rather
	than against the database: every edge target must appear enabled in
	this same save, so one atomic write can add, rewire and retire
	statuses without ever violating the HISI Checklist Status invariants.
	Statuses missing from the payload are left untouched (retire with
	``enabled = 0`` instead of deleting).
	"""
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
	"""Save order that keeps every individual document save valid.

	Start+enabled statuses first (they satisfy the start invariant on
	their own), then other enabled statuses, then disabled ones — by the
	time a disabled status is saved, the payload's enabled start status
	already exists in the database.
	"""
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
	_require_platform()
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

