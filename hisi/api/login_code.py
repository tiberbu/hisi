"""Email one-time-code verification for sign-in.

Every sign-in is completed with a 6-digit code emailed to the account
address: the password proves the account, the code proves the mailbox.
Codes are single-use, expire after ten minutes, tolerate five wrong
guesses, and are stored only as a SHA-256 hash. Issuance and
verification are both guest-reachable, rate-limited, and answer with
identical shapes regardless of whether the account exists, so the
surface never leaks account existence."""

import hashlib
import re
import secrets
import time
from datetime import timedelta

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import get_datetime, now_datetime

from hisi.api.auth import link_for_app
from hisi.utils import has_organization

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
CODE_RE = re.compile(r"^\d{6}$")
CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
EMAIL_SUBJECT = "Your one-time sign-in code for HISI"
REJECT_MESSAGE = "That code did not work. Check the latest email and try again, or request a new code."


def duration_ms(start):
	return int((time.time() - start) * 1000)


def _clean_email(raw):
	email = str(raw or "").strip().lower()
	return email if EMAIL_RE.match(email) else ""


def _code_hash(code):
	return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _display_name(email):
	first = (frappe.db.get_value("User", email, "first_name") or "").strip()
	if first:
		return first
	return email.split("@")[0].title()


def _client_ip():
	request = getattr(frappe.local, "request", None)
	if request is not None:
		return getattr(request, "remote_addr", "") or ""
	return ""


def _send_code_email(email, first_name, code, resend):
	"""Styled sign-in code email (complete HTML, no standard footer). A
	resend tells the reader the earlier code was cancelled."""
	try:
		frappe.sendmail(
			recipients=[email],
			subject=EMAIL_SUBJECT,
			template="login_code",
			args={
				"first_name": first_name,
				"code": code,
				"expires_minutes": int(CODE_TTL.total_seconds() // 60),
				"sign_in_url": link_for_app("/sign-in"),
				"resend": 1 if resend else 0,
			},
			raw_html=True,
			add_unsubscribe_link=0,
		)
	except Exception as exc:
		frappe.log_error(message=str(exc) or repr(exc), title="Login code email failed")


def _revoke_pending(user):
	for row in frappe.get_all("HISI Login Code", filters={"user": user, "status": "Pending"}, fields=["name"]):
		frappe.db.set_value("HISI Login Code", row.name, "status", "Revoked")


def issue_login_code(user: str, resend: bool = False) -> dict:
	"""Insert a fresh Pending code for ``user`` and email it.

	Used by the password step of sign-in (the code is the mandatory MFA
	factor) and by the guest resend path. Cancelling an unspent code is
	the caller's job (see ``_revoke_pending``)."""
	code = "%06d" % secrets.randbelow(1000000)
	doc = frappe.get_doc(
		{
			"doctype": "HISI Login Code",
			"user": user,
			"code_hash": _code_hash(code),
			"status": "Pending",
			"attempts": 0,
			"expires_at": (now_datetime() + CODE_TTL).strftime("%Y-%m-%d %H:%M:%S"),
			"ip": _client_ip(),
		}
	)
	doc.insert(ignore_permissions=True)
	_send_code_email(user, _display_name(user), code, resend)
	return {"sent": True, "expires_in": int(CODE_TTL.total_seconds())}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=5, seconds=300)
def request_login_code(email: str = "") -> dict:
	"""Issue (or re-issue) a one-time sign-in code for an existing account.

	The response is identical whether or not the account exists (no
	enumeration). A fresh request cancels any unspent code for that user;
	when it does, the email says the earlier code was cancelled."""
	start = time.time()
	email = _clean_email(email)
	if not email:
		frappe.throw("Enter a valid email address.", frappe.ValidationError)
	user = frappe.db.get_value("User", {"email": email, "enabled": 1}, "name")
	if not user:
		# Same shape as a real send: account existence is never disclosed.
		return {"sent": True, "expires_in": int(CODE_TTL.total_seconds())}
	resend = bool(frappe.get_all("HISI Login Code", filters={"user": user, "status": "Pending"}, fields=["name"], limit=1))
	_revoke_pending(user)
	return issue_login_code(user, resend)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=300)
def verify_login_code(email: str = "", code: str = "") -> dict:
	"""Exchange the emailed one-time code for a signed-in session.

	All failures return the same generic 401 - account existence and code
	validity are never disclosed - and attempts are rate-limited. A wrong
	code burns one of five attempts before the code is revoked outright."""
	start = time.time()
	email = _clean_email(email)
	code = (code or "").strip()

	def reject():
		frappe.throw(REJECT_MESSAGE, frappe.AuthenticationError)

	if not email or not CODE_RE.match(code):
		reject()
	user = frappe.db.get_value("User", {"email": email, "enabled": 1}, "name")
	if not user:
		reject()
	rows = frappe.get_all(
		"HISI Login Code",
		filters={"user": user, "status": "Pending"},
		fields=["name", "code_hash", "attempts", "expires_at"],
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		reject()
	row = rows[0]
	if row.expires_at and now_datetime() > get_datetime(row.expires_at):
		frappe.db.set_value("HISI Login Code", row.name, "status", "Expired")
		frappe.db.commit()
		reject()
	if row.code_hash != _code_hash(code):
		attempts = int(row.attempts or 0) + 1
		frappe.db.set_value("HISI Login Code", row.name, {"attempts": attempts, "status": "Revoked" if attempts >= MAX_ATTEMPTS else "Pending"})
		frappe.db.commit()
		reject()
	frappe.db.set_value("HISI Login Code", row.name, "status", "Used")
	frappe.local.login_manager.login_as(user)
	# Same login-time mandate as auth.login: a user with no organization
	# gets no usable session - the sign-in surface shows the block message.
	if not has_organization(user):
		frappe.local.login_manager.logout()
		return {
			"status": "no_organization",
			"message": "This account is not linked to an organization. Ask your organization administrator to add you, then sign in again.",
		}
	return {"user": user, "message": "Logged In"}
