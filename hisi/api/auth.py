import os
import re
from urllib.parse import urlparse

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import get_url
from frappe.utils.oauth import get_oauth2_authorize_url
from frappe.utils.password import get_password_reset_limit

from hisi.api.response_handler import APIResponse, BadRequest, api_endpoint
from hisi.utils import active_companies, has_organization

MAX_DISPLAY_NAME_LENGTH = 140
PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9\s\-()]{4,19}$")
GENDER_OPTIONS = ("Male", "Female")

def allowed_redirect_hosts() -> set:
	hosts = {urlparse(get_url()).hostname or ""}
	extra = os.environ.get("HISI_ALLOWED_REDIRECT_HOSTS", "").strip()
	for host in extra.split(","):
		host = host.strip().lower()
		if host:
			hosts.add(host)
	hosts.discard("")
	return hosts

def validated_redirect(redirect_to: str) -> str:
	if not redirect_to:
		return ""
	parsed = urlparse(redirect_to)
	if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
		frappe.throw("redirect_to must be an absolute http(s) URL.", frappe.ValidationError)
	if (parsed.hostname or "").lower() not in allowed_redirect_hosts():
		frappe.throw("redirect_to is not an allowed post-login destination.", frappe.ValidationError)
	return redirect_to

def app_origin() -> str:
	origin = (os.environ.get("HISI_APP_URL") or frappe.conf.get("hisi_app_url") or "").strip().rstrip("/")
	if not origin:
		return ""
	parsed = urlparse(origin)
	if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
		return ""
	return origin

def link_for_app(link: str) -> str:
	origin = app_origin()
	if not origin:
		return link
	parsed = urlparse(link)
	if parsed.scheme not in ("http", "https") or not parsed.hostname:
		return link
	path = parsed.path or "/"
	return origin + path + (("?" + parsed.query) if parsed.query else "")

@frappe.whitelist(allow_guest=True)
def list_social_logins(redirect_to: str = "") -> dict:
	"""Enabled social providers with the label and icon configured on each
	Social Login Key, so the frontend renders them as-is (no hardcoding)."""
	redirect = validated_redirect(redirect_to or "")
	providers = []
	for row in frappe.get_all(
		"Social Login Key",
		filters={"enable_social_login": 1},
		fields=["name", "social_login_provider", "provider_name", "icon"],
		order_by="name asc",
	):
		try:
			providers.append(
				{
					"name": row.name,
					"providerName": row.provider_name or row.social_login_provider or row.name,
					"icon": row.icon or "",
					"authUrl": get_oauth2_authorize_url(row.name, redirect),
				}
			)
		except Exception:
			frappe.log_error(
				title=f"HISI social login unavailable: {row.name}",
				message=frappe.get_traceback(),
			)
	return {"providers": providers}

@frappe.whitelist(allow_guest=True)
def get_session_state() -> dict:
	"""Whether the caller has a signed-in session. Nothing else — the
	frontend resolves identity separately via get_my_profile."""
	return {"logged": frappe.session.user != "Guest"}


@frappe.whitelist()
def get_my_profile() -> dict:
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not authorized", frappe.PermissionError)
	user_row = (
		frappe.db.get_value("User", user, ["full_name", "email"], as_dict=True)
		or frappe._dict()
	)
	roles = [role for role in frappe.get_roles(user) if role not in ("All", "Guest")]
	organizations = []
	for company in active_companies(user):
		organizations.append(
			{"id": company, "name": frappe.db.get_value("Company", company, "company_name") or company, "kind": "company"}
		)
	APIResponse.success(data = {
		"email": user_row.get("email") or user,
		"fullName": user_row.get("full_name") or user,
		"roles": roles,
		"organizations": organizations,
	})

@frappe.whitelist(allow_guest=True, methods=["POST"])
def login(usr: str = "", pwd: str = "", otp: str = "", tmp_id: str = "") -> dict:
	form = frappe.form_dict
	form["usr"] = (usr or "").strip()
	if pwd:
		form["pwd"] = pwd
	if otp:
		form["otp"] = otp.strip()
	if tmp_id:
		form["tmp_id"] = tmp_id.strip()
	outcome = frappe.local.login_manager.login()
	if outcome is False:
		redirect_to = frappe.local.response.get("redirect_to")
		if redirect_to:
			link = link_for_app(redirect_to)
			frappe.local.response["redirect_to"] = link
			try:
				frappe.get_doc("User", frappe.local.login_manager.user).password_reset_mail(link)
			except frappe.OutgoingEmailError:
				frappe.log_error(
					title="Password reset email could not be sent",
					message=frappe.get_traceback(),
				)
			return {
				"sent": True,
				"status": "password_expired",
				"message": "Your password has expired. We emailed a fresh reset link.",
			}
		return {
			"status": "otp_required",
			"tmpId": frappe.local.response.get("tmp_id"),
			"verification": frappe.local.response.get("verification"),
		}

	# Login-time organization mandate: a user that is not tied to any
	# organization (no active Employee on any Company) gets no usable
	# session - the sign-in surface shows the block message instead.
	user = frappe.local.login_manager.user
	if not has_organization(user):
		frappe.local.login_manager.logout()
		return {
			"status": "no_organization",
			"message": "This account is not linked to an organization. Ask your organization administrator to add you, then sign in again.",
		}
	return {"user": user, "message": "Logged In"}

@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=get_password_reset_limit, seconds=60 * 60)
def request_password_reset(user: str) -> dict:
	email = (user or "").strip().lower()
	if not email:
		frappe.throw("Enter a valid email address.", frappe.ValidationError)

	user_name = frappe.db.get_value("User", {"email": email, "enabled": 1}, "name")
	if not user_name:
		return {"sent": True}

	try:
		user_doc = frappe.get_doc("User", user_name)
		user_doc.validate_reset_password()
		link = link_for_app(user_doc._reset_password(send_email=False))
		user_doc.password_reset_mail(link)
	except frappe.DoesNotExistError:
		frappe.clear_messages()
	except frappe.OutgoingEmailError:
		frappe.clear_messages()
		frappe.log_error(title="Password reset email could not be sent", message=frappe.get_traceback())
	except Exception:
		frappe.clear_messages()
		frappe.log_error(title="Password reset request failed unexpectedly", message=frappe.get_traceback())
	return {"sent": True}

@frappe.whitelist(allow_guest=True, methods=["POST"])
def complete_password_reset(key: str, new_password: str) -> dict:
	if not (key or "").strip() or not (new_password or "").strip():
		frappe.throw("A reset key and a new password are required.", frappe.ValidationError)
	from frappe.core.doctype.user.user import update_password

	result = update_password(new_password=new_password, key=(key or "").strip())
	status = frappe.local.response.get("http_status_code")
	if status and int(status) >= 400:
		frappe.local.response["http_status_code"] = 410
		frappe.local.response["message"] = str(result) or "The reset link is no longer valid."
		return None
	return {"reset": True, "redirect": result or "/sign-in"}

@frappe.whitelist(methods=["POST"])
@api_endpoint
def update_profile(full_name: str = "", phone_number: str = "", gender: str = "") -> dict:
	name = (full_name or "").strip()
	phone = (phone_number or "").strip()
	selected_gender = (gender or "").strip()

	if not name and not phone and not selected_gender:
		raise BadRequest("Provide at least one of full_name, phone_number or gender.")
	if name and len(name) > MAX_DISPLAY_NAME_LENGTH:
		raise BadRequest("Display name is too long.")
	if phone and not PHONE_PATTERN.match(phone):
		raise BadRequest(
			"Phone number must be 5-20 characters: digits, spaces, dashes, "
			"parentheses, optionally with a leading +."
		)
	if selected_gender and selected_gender not in GENDER_OPTIONS:
		raise BadRequest("Gender must be either 'Male' or 'Female'.")
	user = frappe.session.user
	doc = frappe.get_doc("User", user)
	changed = False
	if name and doc.full_name != name:
		parts = name.split()
		doc.first_name = parts[0]
		doc.middle_name = " ".join(parts[1:-1]) if len(parts) > 2 else ""
		doc.last_name = parts[-1] if len(parts) > 1 else ""
		changed = True
	if phone and doc.mobile_no != phone:
		doc.mobile_no = phone
		changed = True
	if selected_gender and doc.gender != selected_gender:
		doc.gender = selected_gender
		changed = True
	if changed:
		doc.save(ignore_permissions=True)
		frappe.db.commit()
	return APIResponse.success(data={
		"email": user,
		"fullName": doc.full_name or "",
		"phoneNumber": doc.mobile_no or "",
		"gender": doc.gender or "",
		"updated": changed,
	})
