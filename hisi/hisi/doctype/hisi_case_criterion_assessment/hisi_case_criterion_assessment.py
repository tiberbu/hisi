import frappe
from frappe.model.document import Document


class HISICaseCriterionAssessment(Document):
	def validate(self):
		self.criterion_id = (self.criterion_id or "").strip()
		self._validate_unique_criterion()

	def _validate_unique_criterion(self):
		if not (self.case and self.criterion_id):
			return
		filters = {"case": self.case, "criterion_id": self.criterion_id}
		# A new document may already be named after the row it collides with,
		# so only an existing row is allowed to exclude itself.
		if not self.is_new():
			filters["name"] = ["!=", self.name or ""]
		duplicate = frappe.db.exists("HISI Case Criterion Assessment", filters)
		if duplicate:
			frappe.throw(
				f"Criterion '{self.criterion_id}' already has an assessment on case '{self.case}'.",
				frappe.DuplicateEntryError,
			)
