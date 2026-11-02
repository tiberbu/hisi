"""HISI-branded forgot-password email.

Overrides the framework's ``reset_password`` whitelisted method (see
``hisi/hooks.py``) so the reset email goes out synchronously from the
default outgoing **Email Account**, with subject and body taken from the
``HISI Forgot Password`` **Email Template** record (editable in the Desk) —
instead of the stock framework template, which renders as plain text from
"Administrator" with the "Sent via ERPNext" footer.

The framework still owns the security-critical parts: reset-key generation
(``User._reset_password``), the reset rate limit, and the generic
anti-enumeration response. This module only replaces the email leg.
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils.password import get_password_reset_limit

from hisi.utils import send_reset_email


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=get_password_reset_limit, seconds=60 * 60)
def reset_password(user: str) -> None:
	# Always return the same generic response regardless of whether the user
	# exists, is disabled, or is restricted. This prevents username enumeration
	# via different messages or HTTP status codes (CWE-204).

	try:
		user_doc = frappe.get_doc("User", user)
		if user_doc.name != "Administrator" and user_doc.enabled:
			user_doc.validate_reset_password()
			link = user_doc._reset_password(send_email=False)
			send_reset_email(user_doc, link)
		# For Administrator or disabled users: silently skip — same response below
	except frappe.DoesNotExistError:
		frappe.clear_messages()
	except frappe.OutgoingEmailError:
		frappe.clear_messages()
		frappe.log_error(title="Password reset email could not be sent", message=frappe.get_traceback())
	except Exception:
		frappe.clear_messages()
		frappe.log_error(title="Password reset failed unexpectedly", message=frappe.get_traceback())

	frappe.msgprint(
		msg=_(
			"If this email is registered with us, we have sent password reset instructions to it. Please check your inbox."
		),
		title=_("Password Reset"),
	)
