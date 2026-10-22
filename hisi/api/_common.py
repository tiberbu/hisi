"""Shared helpers for the hisi.api.* BFF surface.

Every public method returns the BFF envelope ``{"status": "success", "data": ...}``
expected by ``app/api/hisi/[operation]/route.ts`` in the HISI portal. Errors are
raised as Frappe exceptions so the HTTP status is meaningful to the BFF:

- ``frappe.PermissionError``  -> 403 (BFF surfaces as 401 / re-authenticate)
- ``frappe.DoesNotExistError`` -> 404 (also used for out-of-tenant records, so
  callers cannot enumerate other organizations' identifiers — CWE-639)
- ``frappe.DuplicateEntryError`` -> 409
- ``frappe.ValidationError``  -> 417 with a user-safe message
"""

import frappe
from frappe import _

from hisi.constants import REVIEWER_ROLES, VENDOR_ROLES
from hisi.utils import get_user_orgs, has_vendor_role, is_platform_user, is_reviewer

CASE = "HISI SHA Validation Case"


def ok(data=None) -> dict:
	return {"status": "success", "data": data if data is not None else {}}


def require_user() -> str:
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Sign in to continue."), frappe.PermissionError)
	return user


def not_found(label: str = "Record"):
	raise frappe.DoesNotExistError(_("{0} not found.").format(_(label)))


def require_reviewer(user: str | None = None):
	if not is_reviewer(user):
		frappe.throw(
			_("Only SHA reviewers or the decision authority can perform this action."),
			frappe.PermissionError,
		)


def clean(value, max_len: int = 1000) -> str:
	"""Trim, strip HTML and clip free text from the portal (stored XSS defence in depth)."""
	if value is None:
		return ""
	text = frappe.utils.strip_html(str(value)).strip()
	return text[:max_len]


def fmt_dt(value) -> str:
	if not value:
		return ""
	return frappe.utils.get_datetime(value).isoformat(timespec="seconds")


def fmt_date(value) -> str:
	if not value:
		return ""
	return str(frappe.utils.getdate(value))


def can_access_vendor(vendor: str, user: str | None = None) -> bool:
	user = user or frappe.session.user
	return is_platform_user(user) or vendor in get_user_orgs(user)


def require_vendor_actor(vendor: str, roles: tuple[str, ...] = VENDOR_ROLES, *, allow_reviewer: bool = False):
	"""Caller must be an active member of `vendor` holding one of `roles`
	(or, when allowed, a reviewer). Out-of-tenant callers get 404."""
	user = require_user()
	if allow_reviewer and is_reviewer(user):
		return user
	if vendor not in get_user_orgs(user):
		if is_platform_user(user):
			frappe.throw(_("This action is reserved for the vendor organization."), frappe.PermissionError)
		not_found("Validation case")
	if not has_vendor_role(user, roles):
		frappe.throw(_("Your role does not permit this action."), frappe.PermissionError)
	return user


def get_case(case_id: str | None):
	"""Load a validation case the caller may read, else 404 (no existence oracle)."""
	user = require_user()
	if not case_id:
		not_found("Validation case")
	vendor = frappe.db.get_value(CASE, case_id, "vendor")
	if not vendor or not can_access_vendor(vendor, user):
		not_found("Validation case")
	return frappe.get_doc(CASE, case_id)


def default_case_name() -> str | None:
	rows = frappe.get_list(CASE, fields=["name"], order_by="modified desc", limit=1)
	return rows[0].name if rows else None


def lock_case(case_id: str):
	"""Serialize concurrent mutations of one case (SELECT ... FOR UPDATE)."""
	frappe.db.get_value(CASE, case_id, "name", for_update=True)


__all__ = [
	"CASE",
	"REVIEWER_ROLES",
	"can_access_vendor",
	"clean",
	"default_case_name",
	"fmt_date",
	"fmt_dt",
	"get_case",
	"lock_case",
	"not_found",
	"ok",
	"require_reviewer",
	"require_user",
	"require_vendor_actor",
]
