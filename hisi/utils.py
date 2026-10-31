import os

import frappe

PLATFORM_ROLES = ("System Manager",)


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


def active_companies(user: str | None = None) -> list:
	"""Companies the user is an active Employee of, in membership order."""
	user = user or frappe.session.user
	if not user or user == "Guest":
		return []
	rows = frappe.get_all(
		"Employee",
		filters={"user_id": user, "status": "Active"},
		fields=["company", "creation"],
		order_by="creation asc",
	)
	companies = []
	for row in rows:
		if row["company"] and row["company"] not in companies:
			companies.append(row["company"])
	return companies


def has_organization(user: str | None = None) -> bool:
	"""True if the user is tied to at least one organization."""
	return bool(active_companies(user))


def ensure_designation(name: str) -> str:
	"""The Designation record for the given name, created on first use."""
	existing = frappe.db.exists("Designation", name)
	if existing:
		return existing
	doc = frappe.get_doc({"doctype": "Designation", "designation_name": name})
	doc.insert(ignore_permissions=True)
	return doc.name


def upsert_org_employee(user: str, company: str, designation: str | None = None):
	"""Create or reactivate the Employee record tying the user to the company.

	Membership in an organization IS an active Employee record on that
	organization's Company. Returns the Employee name, or None when the
	company is empty (nothing to link).
	"""
	if not company:
		return None
	existing = frappe.get_all(
		"Employee", filters={"user_id": user, "company": company}, fields=["name", "status"], limit=1
	)
	if existing:
		if existing[0].status != "Active":
			frappe.db.set_value("Employee", existing[0].name, "status", "Active")
		return existing[0].name
	user_row = (
		frappe.db.get_value("User", user, ["first_name", "last_name", "gender"], as_dict=True)
		or frappe._dict()
	)
	first_name = (user_row.get("first_name") or "").strip() or (user.split("@")[0] or "User").title()
	gender = user_row.get("gender") or ""
	if gender not in ("Male", "Female", "Other"):
		gender = "Other"
	doc = frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": first_name,
			"last_name": user_row.get("last_name") or "",
			"user_id": user,
			"company": company,
			"status": "Active",
			"create_user_permission": 1,
			"gender": gender,
			"date_of_birth": "1900-01-01",
			"date_of_joining": frappe.utils.nowdate(),
		}
	)
	if designation:
		doc.designation = ensure_designation(designation)
	doc.insert(ignore_permissions=True)
	return doc.name


def remove_org_employee(user: str, company: str) -> None:
	"""Deactivate the Employee record (the user account itself survives)."""
	if not company:
		return
	rows = frappe.get_all(
		"Employee", filters={"user_id": user, "company": company, "status": "Active"}, fields=["name"]
	)
	for row in rows:
		frappe.db.set_value("Employee", row.name, "status", "Inactive")


def is_company_member(company: str, user: str | None = None) -> bool:
	"""True if the user is an active Employee on the given Company
	(membership in an organization is an active Employee record)."""
	user = user or frappe.session.user
	if not company or not user or user == "Guest":
		return False
	return bool(
		frappe.get_all(
			"Employee",
			filters={"user_id": user, "company": company, "status": "Active"},
			fields=["name"],
			limit=1,
		)
	)
