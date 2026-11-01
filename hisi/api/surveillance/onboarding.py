import re
import secrets
import time
from datetime import timedelta
from urllib.parse import parse_qs, quote, urlparse

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import now_datetime
from frappe.utils.password import update_password

from hisi.api.auth import link_for_app
from hisi.api.response_handler import APIResponse, BadRequest, Conflict, Forbidden, NotFound, api_endpoint
from hisi.api.surveillance.audit import log_event
from hisi.api.surveillance.mandate import caller_payer_companies
from hisi.utils import upsert_org_employee

OFFICER_DESIGNATION = "HISI Insurance Surveillance"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
CODE_RE = re.compile(r"^\d{6}$")
INVITE_TTL = timedelta(days=7)
EMAIL_SUBJECT = "Your one-time sign-in code for HISI"


def duration_ms(start):
	return int((time.time() - start) * 1000)


def clean_email(raw):
	email = str(raw or "").strip().lower()
	return email if EMAIL_RE.match(email) else ""


def split_name(full_name, email):
	source = (full_name or "").strip() or email.split("@")[0]
	parts = [part for part in source.replace(".", " ").replace("_", " ").split() if part]
	if not parts:
		return "Officer", ""
	if len(parts) == 1:
		return parts[0].title(), ""
	return parts[0].title(), " ".join(parts[1:]).title()


def grant_role(user, role):
	"""Idempotent role attach via the User child-table API. A bare Has Role
	insert fails this build's validation (mandatory parenttype), and the
	build ships no frappe.add_user_role."""
	if not frappe.db.exists("Role", role):
		frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)
	user_doc = frappe.get_doc("User", user)
	current_roles = {row.role for row in user_doc.get("roles")}
	if role not in current_roles:
		# append + save with ignore_permissions: the caller is a payer
		# dispatcher, not a System User, and must not need user-edit rights.
		user_doc.append("roles", {"role": role})
		user_doc.save(ignore_permissions=True)


def _revoke_pending_invites(email, company):
	for row in frappe.get_all(
		"HISI Officer Invite",
		filters={"email": email, "company": company, "status": "Pending"},
		fields=["name"],
	):
		frappe.db.set_value("HISI Officer Invite", row.name, "status", "Revoked")


def _create_invite(email, company, invited_by, account_created):
	code = "%06d" % secrets.randbelow(1000000)
	expires = (now_datetime() + INVITE_TTL).strftime("%Y-%m-%d %H:%M:%S")
	doc = frappe.get_doc(
		{
			"doctype": "HISI Officer Invite",
			"email": email,
			"company": company,
			"code": code,
			"status": "Pending",
			"account_created": 1 if account_created else 0,
			"invited_by": invited_by,
			"expires_at": expires,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def _send_invite_email(email, first_name, company_name, code, sign_in_url, new_account):
	"""Styled invite email (complete HTML, no standard footer). New accounts
	exchange the code for a first password; existing accounts confirm the
	company link and are signed in."""
	try:
		frappe.sendmail(
			recipients=[email],
			subject=EMAIL_SUBJECT,
			template="officer_invite",
			args={
				"first_name": first_name,
				"company": company_name,
				"code": code,
				"sign_in_url": sign_in_url,
				"new_account": 1 if new_account else 0,
			},
			raw_html=True,
			add_unsubscribe_link=0,
		)
	except Exception as exc:
		frappe.log_error(message=str(exc) or repr(exc), title="Officer invite email failed")


def _invite_url(email):
	return link_for_app("/verify-code?email=" + quote(email))


def _display_name(email):
	first = (frappe.db.get_value("User", email, "first_name") or "").strip()
	if first:
		return first
	return email.split("@")[0].title()


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=120, seconds=60)
@api_endpoint
def list_payer_members():
	start = time.time()
	user = frappe.session.user
	orgs = caller_payer_companies(user)
	if not orgs:
		log_event(None, user, "scope_denied", "user", None, outcome="denied", duration_ms=duration_ms(start))
		raise Forbidden("You are not a member of a payer organization.")
	scope_kind = "payer"
	employee_rows = frappe.get_all(
		"Employee",
		filters={"company": ["in", orgs or [""]], "status": "Active"},
		fields=["name", "company", "user_id", "designation", "creation"],
		order_by="creation asc",
	)
	rows = []
	active_counts = {}
	for row in employee_rows:
		if row["company"] in orgs:
			rows.append({"id": row["name"], "parent": row["company"], "user": row["user_id"], "designation": row["designation"], "creation": row["creation"]})
			active_counts[row["company"]] = active_counts.get(row["company"], 0) + 1
	user_names = []
	for row in rows:
		if row["user"] not in user_names:
			user_names.append(row["user"])
	profiles = {}
	if user_names:
		for row in frappe.get_all("User", filters={"name": ["in", user_names]}, fields=["name", "full_name"]):
			profiles[row["name"]] = row["full_name"]
	members = []
	for row in rows:
		roles = [role for role in frappe.get_roles(row["user"]) if role not in ("All", "Guest")]
		members.append(
			{
				"user": row["user"],
				"full_name": profiles.get(row["user"]) or row["user"],
				"roles": roles,
				"officer": row["designation"] == OFFICER_DESIGNATION,
				"company": row["parent"],
				"added_on": str(row["creation"]) if row["creation"] else None,
				"id": row["id"],
				"status": "active",
			}
		)
	# Unconfirmed invitations: one row per invite, unless the user is already
	# an active member of that company. Expired invites stay visible (marked
	# "expired") so the dispatcher can resend a fresh one-time code.
	active_pairs = {(row["user"], row["parent"]) for row in rows}
	now = now_datetime()
	seen_invites = set()
	for inv in frappe.get_all(
		"HISI Officer Invite",
		filters={"company": ["in", orgs or [""]], "status": "Pending"},
		fields=["name", "email", "company", "creation", "expires_at"],
		order_by="creation desc",
	):
		if (inv.email, inv.company) in active_pairs or (inv.email, inv.company) in seen_invites:
			continue
		seen_invites.add((inv.email, inv.company))
		full_name = frappe.db.get_value("User", inv.email, "full_name")
		expired = inv.expires_at and now > frappe.utils.get_datetime(inv.expires_at)
		members.append(
			{
				"user": inv.email,
				"full_name": full_name or inv.email,
				"roles": [],
				"officer": True,
				"company": inv.company,
				"added_on": str(inv.creation) if inv.creation else None,
				"id": inv.name,
				"status": "expired" if expired else "pending",
			}
		)
	log_event(None, user, "members_listed", "user", None, details={"scope": scope_kind, "total": len(members)}, outcome="ok", duration_ms=duration_ms(start))
	return APIResponse.success(
		data={
			"members": members,
			"companies": [
				{
					"company": name,
					"company_name": frappe.db.get_value("Company", name, "company_name") or name,
					"active_members": active_counts.get(name, 0),
				}
				for name in orgs
			],
			"total": len(members),
		}
	)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=20, seconds=60)
@api_endpoint
def onboard_officer(email, full_name=None):
	start = time.time()
	user = frappe.session.user
	email = clean_email(email)

	def fail(message):
		log_event(None, user, "error", "user", email or None, details={"reason": message}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest(message)

	if not email:
		fail("A valid email address is required.")
	orgs = caller_payer_companies(user)
	if not orgs:
		log_event(None, user, "scope_denied", "user", email, outcome="denied", duration_ms=duration_ms(start))
		raise Forbidden("You are not a member of a payer organization.")
	org = orgs[0]
	if frappe.get_all("Employee", filters={"user_id": email, "company": org, "status": "Active"}, fields=["name"], limit=1):
		log_event(None, user, "error", "user", email, details={"reason": "already a member of " + org}, outcome="error", duration_ms=duration_ms(start))
		raise Conflict("That account is already a member of this organization.")
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
		# A placeholder password no one can guess: the emailed code is the
		# only way into a fresh account (it is exchanged for a real password,
		# it is never set as one).
		update_password(email, secrets.token_urlsafe(32))
		user_created = True
	# The newest invite holds the only valid code; earlier unconfirmed
	# invites for this pair are replaced.
	_revoke_pending_invites(email, org)
	invite = _create_invite(email, org, user, account_created=user_created)
	company_name = frappe.db.get_value("Company", org, "company_name") or org
	_send_invite_email(email, _display_name(email), company_name, invite.code, _invite_url(email), user_created)
	log_event(None, user, "officer_onboarded", "user", email, {"user_created": user_created, "company": org, "role": OFFICER_DESIGNATION, "invite": invite.name}, "ok", duration_ms=duration_ms(start))
	return APIResponse.success(
		data={"email": email, "user_created": user_created, "invite_sent": True, "company": org, "status": "pending"}
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=5, seconds=300)
def verify_invite_code(email: str = "", code: str = ""):
	"""One-time code from an officer invite email.

	New accounts: exchange the code for a first-password reset key (the
	frontend then sets the password, which signs the user in). Existing
	accounts: the code confirms the company link and grants a one-time
	sign-in. All failures return the same generic message - account existence
	and code validity are never disclosed - and attempts are rate-limited."""
	start = time.time()
	email = clean_email(email)
	code = (code or "").strip()

	def reject():
		log_event(None, "Guest", "invite_code_rejected", "user", email or None, outcome="denied", duration_ms=duration_ms(start))
		frappe.throw("That code could not be verified. Check the latest invite email and try again.", frappe.ValidationError)

	if not email or not CODE_RE.match(code):
		reject()
	user = frappe.db.get_value("User", {"email": email, "enabled": 1}, "name")
	if not user:
		reject()
	rows = frappe.get_all(
		"HISI Officer Invite",
		filters={"email": email, "status": "Pending", "code": code, "expires_at": [">", now_datetime()]},
		fields=["name", "company", "account_created"],
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		reject()
	invite = rows[0]
	frappe.db.set_value(
		"HISI Officer Invite", invite.name, {"status": "Confirmed", "confirmed_at": now_datetime().strftime("%Y-%m-%d %H:%M:%S")}
	)
	# Confirmation is what links the user to the company (pending until now).
	upsert_org_employee(email, invite.company, designation=OFFICER_DESIGNATION)
	grant_role(email, OFFICER_DESIGNATION)
	log_event(None, user, "invite_confirmed", "user", email, {"company": invite.company, "account_created": bool(invite.account_created)}, "ok", duration_ms=duration_ms(start))
	if invite.account_created:
		link = frappe.get_doc("User", user)._reset_password(send_email=False)
		key = parse_qs(urlparse(link).query).get("key", [""])[0]
		if not key:
			frappe.log_error(message=str(link), title="Officer invite verification could not mint a reset key")
			frappe.throw("Something went wrong verifying that code. Try again.", frappe.ValidationError)
		return {"verified": True, "account": "new", "key": key}
	# Existing account: the confirmed invite is a one-time sign-in grant.
	frappe.local.login_manager.login_as(user)
	return {"verified": True, "account": "existing"}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=20, seconds=60)
@api_endpoint
def resend_officer_invite(email):
	"""Re-issue a pending invite: a fresh one-time code replaces the old one
	and the invite email goes out again."""
	start = time.time()
	user = frappe.session.user
	email = clean_email(email)

	def fail(message):
		log_event(None, user, "error", "user", email or None, details={"reason": message}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest(message)

	if not email:
		fail("A valid email address is required.")
	orgs = caller_payer_companies(user)
	if not orgs:
		log_event(None, user, "scope_denied", "user", email, outcome="denied", duration_ms=duration_ms(start))
		raise Forbidden("You are not a member of a payer organization.")
	org = orgs[0]
	if not frappe.db.get_value("User", {"email": email, "enabled": 1}, "name"):
		log_event(None, user, "error", "user", email, details={"reason": "no account for that email"}, outcome="error", duration_ms=duration_ms(start))
		raise NotFound("No account exists for that email. Invite it first.")
	if frappe.get_all("Employee", filters={"user_id": email, "company": org, "status": "Active"}, fields=["name"], limit=1):
		log_event(None, user, "error", "user", email, details={"reason": "already an active member"}, outcome="error", duration_ms=duration_ms(start))
		raise Conflict("That account is already an active member of this organization.")
	pending = frappe.get_all(
		"HISI Officer Invite",
		filters={"email": email, "company": org, "status": "Pending"},
		fields=["name", "account_created"],
		limit=1,
	)
	if not pending:
		log_event(None, user, "error", "user", email, details={"reason": "no pending invite"}, outcome="error", duration_ms=duration_ms(start))
		raise NotFound("There is no pending invite for that account. Send a new invite first.")
	# Carry the original flag: a fresh account must still exchange its code
	# for a first password after a resend.
	account_created = bool(pending[0].account_created)
	_revoke_pending_invites(email, org)
	invite = _create_invite(email, org, user, account_created=account_created)
	company_name = frappe.db.get_value("Company", org, "company_name") or org
	_send_invite_email(email, _display_name(email), company_name, invite.code, _invite_url(email), account_created)
	log_event(None, user, "officer_invite_resent", "user", email, {"company": org, "invite": invite.name}, "ok", duration_ms=duration_ms(start))
	return APIResponse.success(data={"email": email, "company": org, "invite_sent": True})


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=20, seconds=60)
@api_endpoint
def update_officer(email, full_name):
	"""Rename an officer's display name (an active member, or a pending
	invite, of the caller's organization)."""
	start = time.time()
	user = frappe.session.user
	email = clean_email(email)
	name = str(full_name or "").strip()

	def fail(message):
		log_event(None, user, "error", "user", email or None, details={"reason": message}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest(message)

	if not email:
		fail("A valid email address is required.")
	if not name or len(name) > 140:
		fail("A valid name (1-140 characters) is required.")
	orgs = caller_payer_companies(user)
	if not orgs:
		log_event(None, user, "scope_denied", "user", email, outcome="denied", duration_ms=duration_ms(start))
		raise Forbidden("You are not a member of a payer organization.")
	org = orgs[0]
	member = frappe.get_all("Employee", filters={"user_id": email, "company": org, "status": "Active"}, fields=["name"], limit=1)
	if not member:
		if not frappe.get_all("HISI Officer Invite", filters={"email": email, "company": org, "status": "Pending"}, fields=["name"], limit=1):
			log_event(None, user, "scope_denied", "user", email, outcome="denied", duration_ms=duration_ms(start))
			raise NotFound("That account is not a member of this organization.")
	if not frappe.db.get_value("User", {"email": email, "enabled": 1}, "name"):
		log_event(None, user, "error", "user", email, details={"reason": "no account for that email"}, outcome="error", duration_ms=duration_ms(start))
		raise NotFound("No account exists for that email.")
	user_doc = frappe.get_doc("User", email)
	parts = name.split()
	user_doc.first_name = parts[0]
	user_doc.middle_name = " ".join(parts[1:-1]) if len(parts) > 2 else ""
	user_doc.last_name = parts[-1] if len(parts) > 1 else ""
	user_doc.save(ignore_permissions=True)
	if member:
		employee_doc = frappe.get_doc("Employee", member[0]["name"])
		employee_doc.first_name = user_doc.first_name
		employee_doc.middle_name = user_doc.middle_name
		employee_doc.last_name = user_doc.last_name
		employee_doc.save(ignore_permissions=True)
	log_event(None, user, "officer_updated", "user", email, {"company": org}, "ok", duration_ms=duration_ms(start))
	return APIResponse.success(data={"email": email, "full_name": user_doc.full_name or name})


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=20, seconds=60)
@api_endpoint
def remove_officer(email):
	start = time.time()
	user = frappe.session.user
	email = clean_email(email)

	def fail(message):
		log_event(None, user, "error", "user", email or None, details={"reason": message}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest(message)

	if not email:
		fail("A valid email address is required.")
	orgs = caller_payer_companies(user)
	if not orgs:
		log_event(None, user, "scope_denied", "user", email, outcome="denied", duration_ms=duration_ms(start))
		raise Forbidden("You are not a member of a payer organization.")
	if email == user:
		log_event(None, user, "error", "user", email, details={"reason": "self removal is not allowed from the console"}, outcome="error", duration_ms=duration_ms(start))
		raise Conflict("You cannot remove your own membership. Ask another dispatcher or an administrator.")
	employee_rows = frappe.get_all(
		"Employee",
		filters={"user_id": email, "company": ["in", orgs or [""]], "status": ["in", ["Active", "Inactive"]]},
		fields=["name", "company"],
	)
	# A pending invite is removable the same way (no Employee record yet).
	if not employee_rows:
		invites = frappe.get_all(
			"HISI Officer Invite",
			filters={"email": email, "company": ["in", orgs or [""]], "status": "Pending"},
			fields=["name"],
		)
		if invites:
			for inv in invites:
				frappe.db.set_value("HISI Officer Invite", inv.name, "status", "Revoked")
			log_event(None, user, "officer_removed", "user", email, {"invite_revoked": True}, "ok", duration_ms=duration_ms(start))
			return APIResponse.success(data={"email": email, "removed_from": orgs, "account_kept": True})
		anywhere = frappe.get_all("Employee", filters={"user_id": email, "status": ["in", ["Active", "Inactive"]]}, fields=["company"], limit=1)
		if anywhere:
			log_event(None, user, "scope_denied", "user", email, outcome="denied", duration_ms=duration_ms(start))
			raise Forbidden("You do not have access to this member.")
		log_event(None, user, "error", "user", email, details={"reason": "not a member of a managed organization"}, outcome="error", duration_ms=duration_ms(start))
		raise NotFound("That account is not a member of an organization you manage.")
	# A removal also revokes any unconfirmed invites, so a removed member
	# cannot confirm a stale code later.
	revoked_invites = 0
	for inv in frappe.get_all(
		"HISI Officer Invite",
		filters={"email": email, "company": ["in", orgs or [""]], "status": "Pending"},
		fields=["name"],
	):
		frappe.db.set_value("HISI Officer Invite", inv.name, "status", "Revoked")
		revoked_invites += 1
	parents = []
	for row in employee_rows:
		frappe.db.set_value("Employee", row.name, "status", "Inactive")
		if row.company not in parents:
			parents.append(row.company)
	log_event(None, user, "officer_removed", "user", email, {"payer_orgs": parents, "invite_revoked": revoked_invites}, "ok", duration_ms=duration_ms(start))
	return APIResponse.success(data={"email": email, "removed_from": parents, "account_kept": True})
