"""Authentication and password reset endpoints (hisi.api.auth.*).

Wraps Frappe core password reset flows with standard HISI BFF envelopes.
"""

import frappe
from frappe import _

from hisi.api._common import clean, ok


@frappe.whitelist(allow_guest=True, methods=["POST"])
def request_password_reset(user: str) -> dict:
	"""Initiate a password reset email without username enumeration (CWE-204)."""
	user_clean = clean(user, 254).lower()
	if not user_clean:
		frappe.throw(_("User or email is required."), frappe.ValidationError)

	from frappe.core.doctype.user.user import reset_password

	reset_password(user=user_clean)

	return ok(
		{"message": _("If this email is registered with us, password reset instructions have been sent.")}
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def complete_password_reset(key: str, new_password: str) -> dict:
	"""Complete password reset using the tokenized key sent via email."""
	key_clean = clean(key, 2048)
	if not key_clean or not new_password:
		frappe.throw(_("Key and new password are required."), frappe.ValidationError)

	from frappe.core.doctype.user.user import update_password

	try:
		update_password(new_password=new_password, key=key_clean, logout_all_sessions=1)
	except Exception as exc:
		frappe.throw(str(exc), frappe.ValidationError)

	return ok({"message": _("Password updated successfully.")})
