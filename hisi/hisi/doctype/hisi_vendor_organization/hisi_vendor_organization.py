import frappe
from frappe.model.document import Document


class HISIVendorOrganization(Document):
	def has_permission(self, ptype: str, user: str | None = None) -> bool:
		"""Vendor users can read only their own organization; platform roles see all."""
		user = user or frappe.session.user
		if user == "Guest":
			return False
		if frappe.has_roles("System Manager", "HISI Reviewer", user=user):
			return True
		if ptype != "read":
			return False
		return self.is_member_of(frappe.session.user)

	def is_member_of(self, user: str) -> bool:
		return bool(
			frappe.get_all(
				"HISI Organization Member",
				filters={"parent": self.name, "user": user},
				fields=["name"],
			)
		)
