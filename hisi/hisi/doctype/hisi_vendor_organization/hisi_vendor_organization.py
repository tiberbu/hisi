import frappe
from frappe.model.document import Document

from hisi.utils import is_platform_user

# The single member role that may manage its organization.
MEMBER_ADMIN_ROLE = "Vendor Administrator"


class HISIVendorOrganization(Document):
	# Row-level access is enforced via the has_permission /
	# permission_query_conditions hooks (hisi/permissions.py).
	def validate(self):
		"""Each organization can have only one Vendor Administrator.

		Existing organizations are never frozen by legacy data: the rule is
		enforced when a save would *add* an administrator beyond the one the
		organization already has. Payer validation status changes must follow
		the HISI Checklist Status workflow (allowed next statuses); platform
		users may correct a stuck status directly.
		"""
		self._validate_member_admins()
		self._validate_payer_status_transition()

	def _validate_member_admins(self):
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

	def _validate_payer_status_transition(self):
		new_status = (self.payer_validation_status or "").strip()
		if self.is_new() or not new_status:
			return
		previous = frappe.db.get_value(
			self.doctype, self.name, "payer_validation_status"
		)
		previous = (previous or "").strip()
		if previous == new_status:
			return
		# Platform users may correct a status directly (e.g. after a
		# misconfigured workflow or data fix); the API path always goes
		# through an allowed transition.
		if is_platform_user(frappe.session.user):
			return
		if not frappe.db.get_value("HISI Checklist Status", new_status, "enabled"):
			frappe.throw(
				f"Payer validation status '{new_status}' is disabled.",
				frappe.ValidationError,
			)
		if not previous:
			if not frappe.db.get_value("HISI Checklist Status", new_status, "is_start"):
				frappe.throw(
					"A first payer validation status must be a configured start status.",
					frappe.ValidationError,
				)
			return
		allowed = frappe.db.exists(
			"HISI Checklist Status Transition",
			{"parenttype": "HISI Checklist Status", "parent": previous, "to_status": new_status},
		)
		if not allowed:
			frappe.throw(
				f"'{new_status}' is not an allowed next status from '{previous}'.",
				frappe.ValidationError,
			)
