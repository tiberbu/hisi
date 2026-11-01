"""Provider onboarding console (platform operators only).

Creates HISI Insurance Provider companies and their onboarding accounts
(the organization's admin). The admin confirms the emailed one-time code
on the platform sign-in page, gains the HISI Insurance Provider role plus
an active Employee link, and can then invite their own members through the
regular Team management surface (hisi.api.surveillance.onboarding).

The console itself is restricted to platform roles - a provider admin must
not be able to mint other organizations.
"""

import re
import secrets
import time

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils.password import update_password

from hisi.api.response_handler import APIResponse, BadRequest, Conflict, Forbidden, NotFound, Unauthorized, api_endpoint
from hisi.api.surveillance.audit import log_event
from hisi.api.surveillance.onboarding import (
	OFFICER_DESIGNATION,
	_create_invite,
	_display_name,
	_invite_url,
	_revoke_pending_invites,
	_send_invite_email,
	clean_email,
	split_name,
)

PROVIDER_ROLE = "HISI Insurance Provider"
PLATFORM_ROLES = ("System Manager", "HISI Reviewer")
COMPANY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &'.()-]{1,119}$")


def duration_ms(start):
	return int((time.time() - start) * 1000)


def require_platform_operator() -> str:
	"""Only platform roles may onboard provider organizations."""
	user = frappe.session.user
	if user == "Guest":
		raise Unauthorized("Sign in to access the provider onboarding console.")
	if not set(frappe.get_roles(user)).intersection(PLATFORM_ROLES):
		log_event(None, user, "provider_console_denied", "user", None, outcome="denied")
		raise Forbidden("This console is restricted to platform administrators.")
	return user


def _derive_abbr(name: str) -> str:
	"""Short unique-ish abbreviation for the Company (max 5 chars)."""
	initials = "".join(part[0] for part in re.findall(r"[A-Za-z0-9]+", name) if part[:1].isalpha())
	base = (initials or "C").upper()[:5]
	candidate = base
	suffix = 2
	while frappe.db.get_value("Company", {"abbr": candidate}, "name"):
		candidate = f"{base[:4]}{suffix}"
		suffix += 1
	return candidate


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=120, seconds=60)
@api_endpoint
def list_provider_companies() -> dict:
	"""Every company with its onboarding state: active members, live
	admins, and pending invites (so the operator sees what is in flight)."""
	start = time.time()
	user = require_platform_operator()
	companies = frappe.get_all("Company", fields=["name", "company_name"], order_by="name asc")
	member_counts = {}
	admins_by_company = {}
	for row in frappe.get_all(
		"Employee",
		filters={"status": "Active"},
		fields=["company", "user_id", "designation"],
	):
		if not row["company"]:
			continue
		member_counts[row["company"]] = member_counts.get(row["company"], 0) + 1
		if row["designation"] == PROVIDER_ROLE and row["user_id"]:
			admins_by_company.setdefault(row["company"], []).append(row["user_id"])
	pending_by_company = {}
	for row in frappe.get_all(
		"HISI Officer Invite",
		filters={"status": "Pending"},
		fields=["company", "email", "role"],
	):
		entry = pending_by_company.setdefault(row["company"], {"count": 0, "admins": []})
		entry["count"] += 1
		if row["role"] == PROVIDER_ROLE and row["email"]:
			entry["admins"].append(row["email"])
	rows = []
	for company in companies:
		pending = pending_by_company.get(company["name"], {"count": 0, "admins": []})
		rows.append(
			{
				"company": company["name"],
				"company_name": company["company_name"] or company["name"],
				"active_members": member_counts.get(company["name"], 0),
				"admins": sorted(set(admins_by_company.get(company["name"], []))),
				"admin_ready": bool(admins_by_company.get(company["name"])),
				"pending_invites": pending["count"],
				"pending_admins": sorted(set(pending["admins"])),
			}
		)
	log_event(None, user, "provider_companies_listed", "company", None, details={"total": len(rows)}, outcome="ok", duration_ms=duration_ms(start))
	return APIResponse.success(data={"companies": rows, "total": len(rows)})


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=20, seconds=60)
@api_endpoint
def create_provider_company(company_name: str = "") -> dict:
	"""Create a new provider Company (Kenya defaults, matching the existing
	book of business). Returns 409 when the name is already taken."""
	start = time.time()
	user = require_platform_operator()
	name = str(company_name or "").strip()

	def fail(message):
		log_event(None, user, "error", "company", name or None, details={"reason": message}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest(message)

	if not COMPANY_NAME_RE.match(name):
		fail("A valid company name (2-120 characters: letters, digits, spaces and basic punctuation) is required.")
	if frappe.db.exists("Company", name):
		log_event(None, user, "error", "company", name, details={"reason": "already exists"}, outcome="error", duration_ms=duration_ms(start))
		raise Conflict("A company with that name already exists.")
	doc = frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": name,
			"abbr": _derive_abbr(name),
			"default_currency": "KES",
			"country": "Kenya",
			"valuation_method": "FIFO",
		}
	)
	doc.insert(ignore_permissions=True)
	log_event(None, user, "provider_company_created", "company", name, outcome="ok", duration_ms=duration_ms(start))
	return APIResponse.success(data={"company": name, "company_name": name})


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=20, seconds=60)
@api_endpoint
def onboard_provider_admin(company: str = "", email: str = "", full_name: str = None) -> dict:
	"""Create (or reuse) the onboarding account for a provider company and
	email its one-time sign-in code. Confirmation grants the HISI Insurance
	Provider role and the active Employee link - from there the admin invites
	their own members through the Team surface."""
	start = time.time()
	user = require_platform_operator()
	email = clean_email(email)

	def fail(message):
		log_event(None, user, "error", "user", email or None, details={"reason": message}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest(message)

	if not email:
		fail("A valid email address is required.")
	if not frappe.db.exists("Company", company):
		log_event(None, user, "error", "company", company or None, details={"reason": "unknown company"}, outcome="error", duration_ms=duration_ms(start))
		raise NotFound("Select a valid company.")
	if frappe.get_all("Employee", filters={"user_id": email, "company": company, "status": "Active"}, fields=["name"], limit=1):
		log_event(None, user, "error", "user", email, details={"reason": "already an active member of " + company}, outcome="error", duration_ms=duration_ms(start))
		raise Conflict("That account is already an active member of this company.")
	user_created = False
	if not frappe.db.exists("User", email):
		first_name, last_name = split_name(str(full_name or "").strip(), email)
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first_name,
				"last_name": last_name,
				"user_type": "System User",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		# Placeholder password no one can guess: the emailed code is the only
		# way into a fresh account (exchanged for a real password, never set).
		update_password(email, secrets.token_urlsafe(32))
		user_created = True
	# The newest invite holds the only valid code; earlier unconfirmed
	# invites for this pair (officer or admin) are replaced.
	_revoke_pending_invites(email, company)
	invite = _create_invite(email, company, user, account_created=user_created, role=PROVIDER_ROLE)
	company_name = frappe.db.get_value("Company", company, "company_name") or company
	_send_invite_email(email, _display_name(email), company_name, invite.code, _invite_url(email), user_created, role=PROVIDER_ROLE)
	log_event(
		None,
		user,
		"provider_admin_onboarded",
		"user",
		email,
		{"user_created": user_created, "company": company, "role": PROVIDER_ROLE, "invite": invite.name},
		"ok",
		duration_ms=duration_ms(start),
	)
	return APIResponse.success(
		data={"email": email, "user_created": user_created, "invite_sent": True, "company": company, "status": "pending"}
	)
