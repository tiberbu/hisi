"""Flatten checklist steps and case assessments into standalone documents.

Runs in ``[post_model_sync]`` **after** ``hisi.patches.checklist_statuses``
so migrated steps have an ``in_progress`` status to be tagged with.

The ``istable`` flip never drops columns, so this patch can still read the
old ``parent``/``parenttype``/``parentfield`` values:

1. ``HISI Checklist Criterion Step`` child rows become standalone rows keyed
   by ``checklist_version`` + ``criterion_id`` (read from the criterion row
   they used to hang under) and renamed to their ``format:`` autoname;
2. ``HISI Case Criterion Assessment`` child rows gain their ``case`` link;
3. ``HISI Case Criterion Step Progress`` child rows gain their case,
   assessment and step links (rows whose step no longer exists are dropped —
   they cannot be represented any more);
4. ``HISI Checklist Criterion Step Transition`` (the branching step graph)
   and its DocType are removed.

Idempotent: rows that are already migrated are skipped, and a re-run simply
does nothing. Site-side case rows are never deleted except where they can no
longer be resolved.
"""

import frappe

STEP_DOCTYPE = "HISI Checklist Criterion Step"
ASSESSMENT_DOCTYPE = "HISI Case Criterion Assessment"
PROGRESS_DOCTYPE = "HISI Case Criterion Step Progress"
TRANSITION_DOCTYPE = "HISI Checklist Criterion Step Transition"

# Columns the child tables used to carry; nulled once the row is standalone.
PARENT_COLUMNS = ("parent", "parenttype", "parentfield")


def execute():
	_flatten_steps()
	_flatten_assessments()
	_flatten_progress()
	_drop_transition_doctype()
	frappe.db.commit()


def _columns(doctype: str) -> set[str]:
	if not frappe.db.exists("DocType", doctype):
		return set()
	if not frappe.db.table_exists(doctype):
		return set()
	return set(frappe.db.get_table_columns(doctype))


def _pending_rows(doctype: str) -> list[dict]:
	"""Rows still carrying their child-table parent pointer."""
	if not {"parent"} <= _columns(doctype):
		return []
	return frappe.db.sql(
		f"SELECT * FROM `tab{doctype}`"
		" WHERE parent IS NOT NULL AND parent != '' AND parenttype IS NOT NULL",
		as_dict=True,
	)


def _null_parent_columns(doctype: str, name: str, extra: dict | None = None) -> None:
	values = {column: None for column in PARENT_COLUMNS if column in _columns(doctype)}
	if extra:
		values.update({k: v for k, v in extra.items() if k in _columns(doctype)})
	if not values:
		return
	frappe.db.set_value(doctype, name, values)


def _default_status() -> str:
	"""Status migrated steps are tagged with: ``pending`` (the state a
	checklist sits in before the vendor starts the verification journey) when
	enabled, else ``in_progress``, else the lowest-sort enabled start status."""
	rows = frappe.get_all(
		"HISI Checklist Status",
		filters={"enabled": 1},
		fields=["status_key", "is_start"],
		order_by="sort_order asc, status_key asc",
	)
	if not rows:
		return ""
	keys = [row.status_key for row in rows]
	for preferred in ("pending", "in_progress"):
		if preferred in keys:
			return preferred
	for row in rows:
		if row.is_start:
			return row.status_key
	return keys[0]


def _flatten_steps() -> None:
	rows = _pending_rows(STEP_DOCTYPE)
	if not rows:
		return
	status = _default_status()
	if not status:
		frappe.throw(
			"No enabled checklist status is configured; configure one before migrating steps.",
			frappe.ValidationError,
		)

	for row in rows:
		criterion = frappe.db.get_value(
			"HISI Checklist Criterion",
			row.parent,
			["parent", "criterion_id"],
			as_dict=True,
		)
		if not criterion or not criterion.parent:
			# The criterion row is gone: the step has no checklist any more.
			frappe.db.delete(STEP_DOCTYPE, {"name": row.name})
			continue

		values = {
			"checklist_version": criterion.parent,
			"criterion_id": criterion.criterion_id,
			"checklist_status": status,
			"sort_order": int(row.sort_order or 0),
			"requires_note": int(row.requires_note or 0),
		}
		if "is_start" in _columns(STEP_DOCTYPE):
			# The column is NOT NULL; the field itself is gone from the doctype.
			values["is_start"] = 0

		_null_parent_columns(STEP_DOCTYPE, row.name, values)

		new_name = f"{criterion.parent}-{criterion.criterion_id}-{row.step_key}"
		if new_name != row.name and not frappe.db.exists(STEP_DOCTYPE, new_name):
			frappe.db.sql(
				f"UPDATE `tab{STEP_DOCTYPE}` SET name=%s WHERE name=%s",
				(new_name, row.name),
			)


def _flatten_assessments() -> None:
	for row in _pending_rows(ASSESSMENT_DOCTYPE):
		_null_parent_columns(
			ASSESSMENT_DOCTYPE, row.name, {"case": row.parent, "current_step": None}
		)


def _flatten_progress() -> None:
	for row in _pending_rows(PROGRESS_DOCTYPE):
		assessment = frappe.db.get_value(
			ASSESSMENT_DOCTYPE,
			row.parent,
			["case", "criterion_id"],
			as_dict=True,
		)
		if not assessment or not assessment.case:
			frappe.db.delete(PROGRESS_DOCTYPE, {"name": row.name})
			continue
		checklist_version = frappe.db.get_value(
			"HISI SHA Validation Case", assessment.case, "checklist_version"
		)
		step_name = frappe.db.get_value(
			STEP_DOCTYPE,
			{
				"checklist_version": checklist_version,
				"criterion_id": assessment.criterion_id,
				"step_key": row.step_key,
			},
			"name",
		)
		if not step_name:
			# The step it recorded no longer exists; keep nothing dangling.
			frappe.db.delete(PROGRESS_DOCTYPE, {"name": row.name})
			continue
		_null_parent_columns(
			PROGRESS_DOCTYPE,
			row.name,
			{
				"case": assessment.case,
				"assessment": row.parent,
				"step": step_name,
				"step_label": row.step_label or "",
			},
		)


def _drop_transition_doctype() -> None:
	"""Remove the branching step graph: rows, DocType record and table."""
	if frappe.db.table_exists(TRANSITION_DOCTYPE):
		frappe.db.delete(TRANSITION_DOCTYPE)
	if frappe.db.exists("DocType", TRANSITION_DOCTYPE):
		frappe.delete_doc("DocType", TRANSITION_DOCTYPE, force=True, ignore_missing=True)
	# Frappe keeps the physical table of a removed child doctype around.
	frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{TRANSITION_DOCTYPE}`")
