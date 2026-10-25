"""Flat checklist loaders and completion rules.

Criteria are predefined documents (``HISI Criterion``): a checklist version
only holds a link row per criterion, and its title, statement, criticality
and evidence options are read through ``criteria_of``, which merges them in.

Steps and sub steps are standalone documents (see
``HISI Checklist Criterion Step`` / ``HISI Checklist Criterion Sub Step``)
that point back at their criterion with a composite key — checklist version
+ criterion id — because Frappe only supports one level of child tables and
criteria stay a child row of ``HISI Checklist Version``.

Everything in this module is read-only, dependency-free (frappe only) and
safe to import from doctype controllers, API modules and patches alike.
"""

import frappe

EVIDENCE_TYPES = ("Note", "File", "Note or file")

# Text a criterion carries on ``HISI Criterion`` (never on the version row).
CRITERION_TEXT_FIELDS = ("title", "statement", "is_critical", "evidence_options")

# Progress row state -> "this unit is done".
def is_done(row) -> bool:
	return bool(row) and not int(row.reopened or 0)


def validate_evidence_config(doc) -> None:
	"""Evidence type must be one of the supported options."""
	evidence_type = (doc.evidence_type or "").strip()
	if evidence_type and evidence_type not in EVIDENCE_TYPES:
		frappe.throw(
			f"Unknown evidence type '{evidence_type}'.",
			frappe.ValidationError,
		)


def criteria_of(version: str | None) -> list:
	"""Criterion rows of a checklist version, in list order, merged with the
	predefined criterion each row links to.

	The rows are link-only (``criterion_id`` -> ``HISI Criterion``); the
	merged result exposes ``title``, ``statement``, ``is_critical`` and
	``evidence_options`` alongside the row's own ``name``/``parent``/``idx``,
	so callers keep reading one flat criterion object. A row whose criterion
	is missing still comes back (titled with its id) so a stale link never
	breaks a view.
	"""
	if not version:
		return []
	rows = frappe.get_all(
		"HISI Checklist Criterion",
		filters={"parenttype": "HISI Checklist Version", "parent": version},
		fields=["name", "parent", "parenttype", "parentfield", "idx", "criterion_id"],
		order_by="idx asc",
	)
	return _with_criterion_text(rows)


def _with_criterion_text(rows: list) -> list:
	"""Merge ``HISI Criterion`` text into link-only rows (one extra query)."""
	if not rows:
		return rows
	catalog = {
		row.name: row
		for row in frappe.get_all(
			"HISI Criterion",
			filters={"name": ["in", [row.criterion_id for row in rows]]},
			fields=["name", *CRITERION_TEXT_FIELDS],
		)
	}
	merged = []
	for row in rows:
		entry = dict(row)
		definition = catalog.get(row.criterion_id)
		if definition:
			entry.update({field: definition.get(field) for field in CRITERION_TEXT_FIELDS})
		else:
			entry.update(
				{
					"title": row.criterion_id,
					"statement": "",
					"is_critical": 0,
					"evidence_options": "",
				}
			)
		merged.append(frappe._dict(entry))
	return merged


def steps_of(version: str | None, criterion_id: str | None = None) -> list:
	"""Ordered steps of a checklist version (optionally one criterion)."""
	if not version:
		return []
	filters: dict = {"checklist_version": version}
	if criterion_id:
		filters["criterion_id"] = criterion_id
	return frappe.get_all(
		"HISI Checklist Criterion Step",
		filters=filters,
		fields=["*"],
		order_by="sort_order asc, step_key asc",
	)


def sub_steps_of(step_names: list[str] | None) -> dict[str, list]:
	"""Ordered sub steps grouped by parent step name."""
	grouped: dict[str, list] = {}
	if not step_names:
		return grouped
	for row in frappe.get_all(
		"HISI Checklist Criterion Sub Step",
		filters={"step": ["in", step_names]},
		fields=["*"],
		order_by="sort_order asc, sub_step_key asc",
	):
		grouped.setdefault(row.step, []).append(row)
	return grouped


def step_graph(version: str | None) -> tuple[dict[str, list], dict[str, list]]:
	"""Two queries: ``{criterion_id: [steps]}`` and ``{step name: [sub steps]}``."""
	steps = steps_of(version)
	by_criterion: dict[str, list] = {}
	for step in steps:
		by_criterion.setdefault(step.criterion_id, []).append(step)
	return by_criterion, sub_steps_of([step.name for step in steps])


def assessments_of(case_name: str) -> dict[str, object]:
	"""Case assessments by criterion id."""
	return {
		row.criterion_id: row
		for row in frappe.get_all(
			"HISI Case Criterion Assessment",
			filters={"case": case_name},
			fields=["*"],
		)
	}


def progress_rows(case_name: str) -> list:
	return frappe.get_all(
		"HISI Case Criterion Step Progress",
		filters={"case": case_name},
		fields=["*"],
	)


def progress_map(rows: list | None = None, case_name: str | None = None) -> dict[tuple, object]:
	"""Progress rows keyed by ``(step name, sub step name or "")``."""
	if rows is None:
		rows = progress_rows(case_name)
	mapped: dict[tuple, object] = {}
	for row in rows:
		mapped[(row.step, row.sub_step or "")] = row
	return mapped


def evidence_rule(step, sub_step=None) -> tuple[int, str, str]:
	"""``(required, evidence_type, hint)`` for the unit being completed.

	A sub step always wins; a step's own fields apply only when it has no
	sub steps (``sub_step`` omitted).
	"""
	source = sub_step if sub_step is not None else step
	required = int(getattr(source, "evidence_required", 0) or 0)
	evidence_type = (getattr(source, "evidence_type", None) or "Note").strip() or "Note"
	hint = (getattr(source, "evidence_hint", None) or "").strip()
	return required, evidence_type, hint


def step_done(step, sub_steps: list, progress: dict) -> bool:
	"""A step is complete when all of its sub steps are (or, without sub
	steps, when its own progress row exists and is not reopened)."""
	if sub_steps:
		return all(is_done(progress.get((step.name, row.name))) for row in sub_steps)
	return is_done(progress.get((step.name, "")))


def case_context(case_name: str, checklist_version: str | None = None) -> dict:
	"""Everything needed to judge a case's step progress, loaded once.

	``graph`` and ``ordered_ids`` depend only on the checklist version, so
	callers listing many cases can reuse them across cases of one version.
	"""
	if checklist_version is None:
		checklist_version = frappe.db.get_value(
			"HISI SHA Validation Case", case_name, "checklist_version"
		)
	return {
		"version": checklist_version,
		"graph": step_graph(checklist_version),
		"ordered_ids": [row.criterion_id for row in criteria_of(checklist_version)],
		"progress": progress_map(case_name=case_name),
	}


def open_criteria(graph, ordered_ids, progress) -> list[str]:
	"""Criterion ids, in checklist order, whose steps are not all complete."""
	by_criterion, sub_steps = graph
	open_ids: list[str] = []
	for criterion_id in ordered_ids:
		steps = by_criterion.get(criterion_id) or []
		if steps and not all(
			step_done(step, sub_steps.get(step.name, []), progress) for step in steps
		):
			open_ids.append(criterion_id)
	return open_ids


def status_completion(case_name: str, checklist_version: str | None = None) -> dict[str, dict]:
	"""Per checklist status: how far the case's steps are worked through.

	Only steps of criteria that still exist on the case's checklist version
	count, so a criterion removed from an in-place-edited checklist never
	breaks the rollup. Returns ``{status_key: {...}}`` for every status.
	"""
	context = case_context(case_name, checklist_version)
	by_criterion, sub_steps = context["graph"]
	progress = context["progress"]
	live = set(context["ordered_ids"])

	statuses = frappe.get_all(
		"HISI Checklist Status",
		fields=["name", "status_key", "label", "sort_order", "enabled"],
		order_by="sort_order asc, status_key asc",
	)
	completion: dict[str, dict] = {}
	for status in statuses:
		completion[status.status_key] = {
			"statusKey": status.status_key,
			"label": status.label or "",
			"sortOrder": int(status.sort_order or 0),
			"enabled": bool(status.enabled),
			"required": 0,
			"completed": 0,
			"complete": True,
			"remaining": [],
		}

	for criterion_id, steps in by_criterion.items():
		if criterion_id not in live:
			continue
		for step in steps:
			bucket = completion.get(step.checklist_status)
			if bucket is None:
				bucket = completion[step.checklist_status] = {
					"statusKey": step.checklist_status,
					"label": step.checklist_status,
					"sortOrder": 9999,
					"enabled": True,
					"required": 0,
					"completed": 0,
					"complete": True,
					"remaining": [],
				}
			bucket["required"] += 1
			if step_done(step, sub_steps.get(step.name, []), progress):
				bucket["completed"] += 1
			else:
				bucket["remaining"].append(f"{criterion_id}/{step.step_key}")

	for bucket in completion.values():
		bucket["complete"] = bucket["completed"] >= bucket["required"]
	return completion
