import frappe
from frappe.model.document import Document


class HISISHAValidationCase(Document):
	def has_permission(self, ptype: str, user: str | None = None) -> bool:
		"""Vendor users can read only cases for their own organization."""
		user = user or frappe.session.user
		if user == "Guest":
			return False
		if frappe.has_roles("System Manager", "HISI Reviewer", user=user):
			return True
		if ptype != "read":
			return False
		return bool(
			frappe.get_all(
				"HISI Organization Member",
				filters={"parent": self.vendor, "user": frappe.session.user},
				fields=["name"],
			)
		)
