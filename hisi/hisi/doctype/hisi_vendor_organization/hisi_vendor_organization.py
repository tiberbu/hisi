import frappe
from frappe.model.document import Document

# The single member role that may manage its organization.
MEMBER_ADMIN_ROLE = "Vendor Administrator"


class HISIVendorOrganization(Document):
	# Row-level access is enforced via the has_permission /
	# permission_query_conditions hooks (hisi/permissions.py).
	def validate(self):
		"""Each organization can have only one Vendor Administrator.

		Existing organizations are never frozen by legacy data: the rule is
		enforced when a save would *add* an administrator beyond the one the
		organization already has.
		"""
		admins = [row for row in self.get("members") or [] if row.member_role == MEMBER_ADMIN_ROLE]
		if len(admins) <= 1:
			return
		existing = 0
		if not self.is_new():
			existing = frappe.db.count(
				"HISI Organization Member",
				filters={
					"parent": self.name,
					"parenttype": "HISI Vendor Organization",
					"member_role": MEMBER_ADMIN_ROLE,
				},
			)
		if len(admins) > existing:
			frappe.throw(
				"Each organization can have only one Vendor Administrator.",
				frappe.ValidationError,
			)
