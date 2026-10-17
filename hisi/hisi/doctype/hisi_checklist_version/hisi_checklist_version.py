import frappe
from frappe.model.document import Document


class HISIChecklistVersion(Document):
	def validate(self) -> None:
		self.enforce_immutability_once_published()

	def enforce_immutability_once_published(self) -> None:
		"""HISI-FR-011: a published checklist release is immutable."""
		if self.is_new():
			return
		was_published = bool(self.db_value("published"))
		if was_published and self.has_value_changed(["version", "criteria"]):
			frappe.throw(
				f"Checklist version {self.name} is already published and cannot be modified. Create a new version.",
				title="Published checklist is immutable",
			)
