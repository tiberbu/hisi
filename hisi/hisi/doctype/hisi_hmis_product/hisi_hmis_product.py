import frappe
from frappe.model.document import Document


class HISIHMISProduct(Document):
	def has_permission(self, ptype: str, user: str | None = None) -> bool:
		"""Vendor users can read only products of their own organization."""
		user = user or frappe.session.user
		if user == "Guest":
			return False
		if frappe.has_roles("System Manager", "HISI Reviewer", user=user):
			return True
		if ptype != "read":
			return False
		return frappe.db.get_value("HISI Vendor Organization", self.vendor, "name") == self.vendor and bool(
			frappe.get_all(
				"HISI Organization Member",
				filters={"parent": self.vendor, "user": frappe.session.user},
				fields=["name"],
			)
		)
