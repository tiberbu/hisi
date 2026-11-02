import os

import frappe

PLATFORM_ROLES = ("System Manager",)


def get_outgoing_sender() -> str | None:
	"""Sender for HISI transactional email, from the default outgoing
	**Email Account** (display name + address) — never hardcoded.

	Falls back to the site-config ``mail_login_id``, then to ``None`` so
	Frappe's own default-account resolution applies.
	"""
	row = frappe.get_all(
		"Email Account",
		filters={"default_outgoing": 1, "enable_outgoing": 1},
		fields=["name", "email_id"],
		limit=1,
	)
	if row and row[0].email_id:
		return f"{row[0].name} <{row[0].email_id}>"
	login_id = frappe.conf.get("mail_login_id")
	if login_id:
		return str(login_id)
	return None


def render_email_template(name: str, args: dict | None = None) -> tuple[str, str]:
	"""Load an **Email Template** record (editable from the Desk) and render
	its HTML with ``args``.

	The record is the single source of truth for subject and body; the
	sender is resolved separately via :func:`get_outgoing_sender`.
	Returns ``(subject, html)``.
	"""
	doc = frappe.get_doc("Email Template", name)
	html = frappe.render_template(doc.response_html or "", args or {})
	return ((doc.subject or "").strip() or name, html)


def send_reset_email(user_doc, link: str) -> None:
	"""Send the branded password-reset email.

	Subject/body from the ``HISI Forgot Password`` **Email Template** record;
	sender from the default outgoing **Email Account** (see
	:func:`get_outgoing_sender`). Shared by the framework ``reset_password``
	override and the app's ``request_password_reset`` endpoint.
	"""
	first_name = (user_doc.first_name or user_doc.last_name or "").strip()
	subject, html = render_email_template(
		"HISI Forgot Password",
		{
			"first_name": first_name or "there",
			"link": link,
		},
	)
	frappe.sendmail(
		recipients=[user_doc.email],
		sender=get_outgoing_sender(),
		subject=subject,
		message=html,
		raw_html=True,
		add_unsubscribe_link=0,
		now=True,
	)


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
	# Employee's user-permission hooks (create_user_permission) run as the
	# current user, but API callers (dispatchers/officers) have no User
	# Permission rights; the bookkeeping insert must run as Administrator.
	previous_user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		doc.insert(ignore_permissions=True)
	finally:
		frappe.set_user(previous_user)
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
