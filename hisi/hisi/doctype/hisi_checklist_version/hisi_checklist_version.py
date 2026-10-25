from frappe.model.document import Document


class HISIChecklistVersion(Document):
	"""Single-checklist model: the platform edits the published checklist
	in place; every edit is recorded in the change log, which is the audit
	trail. A separate create-and-publish versioning flow is a future option.

	Criteria stay a child table here (one legal level), as link-only rows to
	the predefined ``HISI Criterion`` they reference. Their steps and
	sub steps live outside as ``HISI Checklist Criterion Step`` /
	``HISI Checklist Criterion Sub Step`` documents keyed by
	``checklist_version`` + ``criterion_id`` — see ``hisi.checklist``.
	"""
