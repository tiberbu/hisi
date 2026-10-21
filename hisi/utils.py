import frappe

from hisi.constants import CROSS_ORG_READ_ROLES, REVIEWER_ROLES, VENDOR_ROLES

# Roles that read across every vendor organization (reviewers, auditors, admins).
PLATFORM_ROLES = CROSS_ORG_READ_ROLES


def _roles(user: str) -> set[str]:
	return set(frappe.get_roles(user))


def is_platform_user(user: str | None = None) -> bool:
	"""True if the user holds a platform (non-vendor, cross-organization) role."""
	user = user or frappe.session.user
	if not user or user == "Guest":
		return False
	return bool(_roles(user) & set(PLATFORM_ROLES))


def is_reviewer(user: str | None = None) -> bool:
	"""True if the user may assess criteria and issue SHA dispositions."""
	user = user or frappe.session.user
	if not user or user == "Guest":
		return False
	return bool(_roles(user) & set(REVIEWER_ROLES))


def has_vendor_role(user: str | None = None, roles: tuple[str, ...] = VENDOR_ROLES) -> bool:
	user = user or frappe.session.user
	if not user or user == "Guest":
		return False
	return bool(_roles(user) & set(roles))


def get_user_orgs(user: str | None = None) -> list[str]:
	"""Vendor organizations where the user is an *active* member."""
	user = user or frappe.session.user
	if not user or user == "Guest":
		return []
	return frappe.get_all(
		"HISI Organization Member",
		filters={"user": user, "status": "Active", "parenttype": "HISI Vendor Organization"},
		pluck="parent",
		order_by="creation asc",
	)


def is_org_member(vendor: str, user: str | None = None) -> bool:
	"""True if the user is an active member of the given HISI Vendor Organization."""
	user = user or frappe.session.user
	if not vendor or not user or user == "Guest":
		return False
	return vendor in get_user_orgs(user)
