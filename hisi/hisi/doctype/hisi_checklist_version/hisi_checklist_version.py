import frappe
from frappe.model.document import Document


class HISIChecklistVersion(Document):
	def validate(self) -> None:
		self.enforce_immutability_once_published()

	def enforce_immutability_once_published(self) -> None:
		"""HISI-FR-011: a published checklist release is immutable."""
		if self.is_new():
			return
		was_published = bool(frappe.db.get_value(self.doctype, self.name, "published"))
		changed = self.has_value_changed("version") or self.has_value_changed("criteria")
		if was_published and changed:
			frappe.throw(
				f"Checklist version {self.name} is already published and cannot be modified. Create a new version.",
				title="Published checklist is immutable",
			)
