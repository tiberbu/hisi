"""Turn checklist criteria into predefined ``HISI Criterion`` documents.

Runs in ``[post_model_sync]`` **after** ``hisi.patches.checklist_flat_steps``
so the flattened steps and assessments already carry their criterion ids.

Removing ``title``/``statement``/``is_critical``/``evidence_options`` from
``HISI Checklist Criterion`` never drops the columns, so this patch can still
read the text the rows used to carry:

1. one ``HISI Criterion`` per distinct id, taking its text from the newest
   checklist version that still carries that criterion;
2. ids that only a case assessment or a step still references get a definition
   as well (title from the assessment, else the id), so no link dangles;
3. the text columns of the membership rows are cleared — the row is a link
   and order now, and reads must never pick up the leftovers.

Idempotent: an existing ``HISI Criterion`` is never rewritten (an administrator
may already have edited it), and a re-run simply does nothing.
"""

import frappe

ENTRY_DOCTYPE = "HISI Checklist Criterion"
CRITERION_DOCTYPE = "HISI Criterion"
VERSION_DOCTYPE = "HISI Checklist Version"
STEP_DOCTYPE = "HISI Checklist Criterion Step"
ASSESSMENT_DOCTYPE = "HISI Case Criterion Assessment"

# Text the criterion row used to carry; the definition carries it now.
TEXT_COLUMNS = ("title", "statement", "evidence_options", "is_critical")

TITLE_LENGTH = 140


def execute():
	_create_definitions()
	_clear_entry_text()
	frappe.db.commit()


def _columns(doctype: str) -> set[str]:
	if not frappe.db.exists("DocType", doctype):
		return set()
	if not frappe.db.table_exists(doctype):
		return set()
	return set(frappe.db.get_table_columns(doctype))


def _definitions_from_entries(definition: dict[str, dict]) -> None:
	"""Newest checklist version wins for a criterion listed on several."""
	if not frappe.db.table_exists(ENTRY_DOCTYPE):
		return
	if not {"criterion_id"} <= _columns(ENTRY_DOCTYPE):
		return
	columns = [column for column in TEXT_COLUMNS if column in _columns(ENTRY_DOCTYPE)]
	selected = ", ".join(f"e.{column}" for column in columns) or "NULL AS title"
	rows = frappe.db.sql(
		f"SELECT e.criterion_id, {selected},"
		" v.effective_on, v.version, v.creation"
		f" FROM `tab{ENTRY_DOCTYPE}` e"
		f" INNER JOIN `tab{VERSION_DOCTYPE}` v ON v.name = e.parent"
		" WHERE e.parenttype = 'HISI Checklist Version'"
		" AND e.criterion_id IS NOT NULL AND e.criterion_id != ''"
		" ORDER BY v.effective_on asc, v.version asc, v.creation asc",
		as_dict=True,
	)
	for row in rows:
		definition[row.criterion_id] = {
			"title": row.get("title"),
			"statement": row.get("statement") or "",
			"evidence_options": row.get("evidence_options") or "",
			"is_critical": int(row.get("is_critical") or 0),
		}


def _definitions_from_cases(definition: dict[str, dict]) -> None:
	"""Keep links of already-assessed criteria resolvable."""
	if not frappe.db.exists("DocType", ASSESSMENT_DOCTYPE):
		return
	for row in frappe.get_all(
		ASSESSMENT_DOCTYPE,
		filters={"criterion_id": ["is", "set"]},
		fields=["criterion_id", "criterion_title"],
	):
		definition.setdefault(
			row.criterion_id,
			{
				"title": row.criterion_title,
				"statement": "",
				"evidence_options": "",
				"is_critical": 0,
			},
		)
	if not frappe.db.exists("DocType", STEP_DOCTYPE):
		return
	for row in frappe.get_all(
		STEP_DOCTYPE,
		filters={"criterion_id": ["is", "set"]},
		fields=["criterion_id"],
	):
		definition.setdefault(
			row.criterion_id,
			{"title": row.criterion_id, "statement": "", "evidence_options": "", "is_critical": 0},
		)


def _create_definitions() -> None:
	definition: dict[str, dict] = {}
	_definitions_from_entries(definition)
	_definitions_from_cases(definition)

	for criterion_id, values in definition.items():
		values["title"] = ((values.get("title") or "").strip() or criterion_id)[:TITLE_LENGTH]
		values["statement"] = (values.get("statement") or "").strip()
		values["evidence_options"] = (values.get("evidence_options") or "").strip()
		values["is_critical"] = 1 if values.get("is_critical") else 0
		if frappe.db.exists(CRITERION_DOCTYPE, criterion_id):
			continue
		frappe.get_doc(
			{"doctype": CRITERION_DOCTYPE, "criterion_id": criterion_id, **values}
		).insert(ignore_if_duplicate=True, ignore_permissions=True)


def _clear_entry_text() -> None:
	"""Membership rows are link-only; drop the text columns left behind."""
	columns = [column for column in TEXT_COLUMNS if column in _columns(ENTRY_DOCTYPE)]
	if not columns:
		return
	assignments = ", ".join(
		f"`{column}` = {0 if column == 'is_critical' else 'NULL'}" for column in columns
	)
	frappe.db.sql(f"UPDATE `tab{ENTRY_DOCTYPE}` SET {assignments}")
