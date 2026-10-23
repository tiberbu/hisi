import os

import frappe

PLATFORM_ROLES = ("System Manager", "HISI Reviewer")


def get_dha_verify_url() -> str:
	"""DHA register base URL: HISI Settings, then site config, then environment.

	Returns ``""`` when nothing usable is configured so callers pick their own
	failure mode (registration fails closed; deep links fall back to the public
	portal). Fail-closed validation: only http(s) URLs without credentials or
	a fragment are accepted.
	"""
	stored = (frappe.get_single("HISI Settings").get("dha_verify_url") or "").strip()
	configured = stored or (frappe.conf.get("hisi_dha_verify_url") or os.environ.get("HISI_DHA_VERIFY_URL") or "").strip()
	if not configured.startswith(("http://", "https://")):
		return ""
	if "@" in configured or "#" in configured:
		return ""
	return configured.rstrip("/")


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
