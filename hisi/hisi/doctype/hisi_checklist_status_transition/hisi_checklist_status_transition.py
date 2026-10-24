from frappe.model.document import Document


class HISIChecklistStatusTransition(Document):
	"""One allowed next status of the parent HISI Checklist Status.

	Rows are managed through the parent status document; validation of the
	transition graph (no self-loops, no duplicates, enabled targets) lives in
	HISI Checklist Status.validate.
	"""

	pass
