import frappe

PLATFORM_ROLES = ("System Manager", "HISI Reviewer")


def is_platform_user(user: str | None = None) -> bool:
	"""True if the user holds a platform (non-vendor) role."""
	user = user or frappe.session.user
	if not user or user == "Guest":
		return False
	return bool(set(frappe.get_roles(user)) & set(PLATFORM_ROLES))


def is_org_member(vendor: str, user: str | None = None) -> bool:
	"""True if the user is a member of the given HISI Vendor Organization."""
	user = user or frappe.session.user
	if not vendor or not user or user == "Guest":
		return False
	return bool(
		frappe.get_all(
			"HISI Organization Member",
			filters={"parent": vendor, "user": user},
			fields=["name"],
		)
	)
