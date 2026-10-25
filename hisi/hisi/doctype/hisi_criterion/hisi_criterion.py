import frappe
from frappe.model.document import Document


class HISICriterion(Document):
	"""Predefined certification criterion (CC-01 ...), the single source of
	truth for a criterion's title, statement, criticality and evidence
	options.

	A checklist version's criterion row only links here, as do its steps and
	each case assessment, so editing a criterion updates every view that
	references it.
	"""

	def validate(self):
		self.criterion_id = (self.criterion_id or "").strip()
		self.title = (self.title or "").strip()
		if not self.criterion_id or not self.title:
			frappe.throw(
				"Criterion ID and Title are required.",
				frappe.ValidationError,
			)
