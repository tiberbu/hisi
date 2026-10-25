"""Guest-facing vendor onboarding for the HISI sign-up flow.

``register_vendor_account`` re-verifies the DHA certificate server-side
(the browser check is advisory only), then creates a *new* vendor
organization, HMIS product, DHA certification reference and the sign-in
user. Registration never joins an existing organization: the registrant
becomes the organization's single Vendor Administrator, and every other
member is onboarded after sign-in by that Vendor Administrator via
``add_organization_member``.

Sign-up can never mint platform administrators: new users receive only the
``HISI Vendor`` role; organization membership supplies the member role.
The DHA register base URL lives in the *HISI Settings* single doctype
(``dha_verify_url``), with site configuration and the host environment as
fallbacks — never in the request. ``verify_dha_certificate`` exposes the
same advisory check to the sign-up form; it is guest-accessible but never
grants access.
"""

import re

import frappe
import requests
from frappe.utils.password import update_password
from hisi.utils import get_dha_verify_url

CERTIFICATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{3,63}$")
VENDOR_ROLE = "HISI Vendor"
VERIFY_TIMEOUT_SECONDS = 10

# Every organization has exactly one Vendor Administrator; all other
# members are onboarded by that administrator under one of these roles.
MEMBER_ADMIN_ROLE = "Vendor Administrator"
MEMBER_ROLES = (
	MEMBER_ADMIN_ROLE,
	"Vendor Technical Lead",
	"Vendor Clinical / Operations Lead",
)

# `HISI DHA Certification Reference.certificate_status` <- register status.
STATUS_MAP = {
	"Active": "Valid",
	"Valid": "Valid",
	"Conditional": "Conditional",
	"Expired": "Expired",
	"Suspended": "Suspended",
}


def _dha_verify_base() -> str:
	"""DHA register base URL from HISI Settings; fails closed when unusable."""
	configured = get_dha_verify_url()
	if not configured:
		frappe.throw("DHA verification is not configured.", frappe.ValidationError)
	return configured


def _date_or_none(value) -> str | None:
	"""Register dates may be ISO datetimes (`2026-10-12T09:50:22.625Z`);
	normalize to `YYYY-MM-DD` for Frappe Date fields."""
	text = (value or "").strip()
	if len(text) >= 10 and text[4] == "-" and text[7] == "-":
		return text[:10]
	return None


def _start_status_key() -> str:
	"""The enabled workflow start status for new organizations, or ``""``
	when none is configured. ``in_review`` by default — sign-up must not
	hard-code a key the site may have renamed or disabled."""
	rows = frappe.get_all(
		"HISI Checklist Status",
		filters={"enabled": 1, "is_start": 1},
		fields=["status_key"],
		order_by="sort_order asc",
		limit_page_length=1,
	)
	return rows[0].status_key if rows else ""


def _verify_certificate(certificate_id: str) -> dict:
	"""Verify one certificate against the DHA register; raises on any failure."""
	cert = (certificate_id or "").strip()
	if not CERTIFICATE_ID_PATTERN.match(cert):
		frappe.throw("Enter a valid DHA certificate ID.", frappe.ValidationError)

	target = f"{_dha_verify_base()}/{cert}"
	try:
		response = requests.get(
			target,
			headers={"accept": "application/json"},
			timeout=VERIFY_TIMEOUT_SECONDS,
			allow_redirects=False,
		)
	except requests.RequestException:
		frappe.throw("The DHA register could not be reached. Try again shortly.", frappe.ValidationError)

	if response.status_code == 404:
		frappe.throw("This DHA certificate was not found on the register.", frappe.ValidationError)
	if response.status_code != 200:
		frappe.throw("The DHA register could not be reached. Try again shortly.", frappe.ValidationError)

	try:
		body = response.json()
	except ValueError:
		frappe.throw("The DHA register returned an unexpected response.", frappe.ValidationError)

	data = body.get("data") if isinstance(body, dict) else None
	if body.get("success") is not True or not isinstance(data, dict) or data.get("found") is not True:
		frappe.throw("This DHA certificate was not found on the register.", frappe.ValidationError)

	is_expired = data.get("isExpired") is True
	is_revoked = data.get("isRevoked") is True
	if data.get("isValid") is not True or is_expired or is_revoked:
		frappe.throw(data.get("message") or "This DHA certificate is not valid.", frappe.ValidationError)

	return {
		"certificate_number": data.get("certificateNumber") or cert,
		"organization_name": data.get("organizationName") or "",
		"system_name": data.get("systemName") or "",
		"system_version": data.get("systemVersion") or "",
		"status": data.get("status") or "Valid",
		"issued_date": _date_or_none(data.get("issuedDate")),
		"expiry_date": _date_or_none(data.get("expiryDate")),
		"message": data.get("message") or "",
	}


@frappe.whitelist(allow_guest=True)
def verify_dha_certificate(certificate_id: str) -> dict:
	"""Advisory pre-registration DHA certificate check for the sign-up form.

	Guest-facing: lets the browser confirm a certificate exists and is valid
	before account creation. The result never grants access —
	``register_vendor_account`` re-verifies server-side against the register
	base URL configured in *HISI Settings*.
	"""
	record = _verify_certificate(certificate_id)
	return {
		"certificateId": record["certificate_number"],
		"organizationName": record["organization_name"],
		"systemName": record["system_name"],
		"systemVersion": record["system_version"] or None,
		"status": record["status"],
		"isValid": True,
		"isExpired": False,
		"isRevoked": False,
		"issuedDate": record["issued_date"],
		"expiryDate": record["expiry_date"],
		"message": record["message"],
	}


@frappe.whitelist(allow_guest=True)
def register_vendor_account(
	certificate_id: str,
	system_name: str,
	release_version: str,
	organization_name: str,
	full_name: str,
	email: str,
	password: str = "",
) -> dict:
	"""Create a new vendor organization account after a live DHA verification.

	Guest-facing: every input is validated here (the Next.js contract is
	defence in depth, not the trust boundary). Rolls back atomically on any
	failure. Registration only ever creates a *new* organization — the
	registrant becomes its single Vendor Administrator. Members of an
	existing organization are onboarded after sign-in by that
	organization's Vendor Administrator. New users hold only the
	``HISI Vendor`` role.
	"""
	email = (email or "").strip()
	full_name = (full_name or "").strip()
	organization_name = (organization_name or "").strip()
	system_name = (system_name or "").strip()
	release_version = (release_version or "").strip()

	if not full_name or not organization_name or not system_name or not release_version:
		frappe.throw("Missing required sign-up details.", frappe.ValidationError)
	if not password or len(password) < 8:
		frappe.throw("Password does not meet the minimum length.", frappe.ValidationError)
	frappe.utils.validate_email_address(email, throw=True)

	# Registration always creates the organization's single administrator.
	member_role = MEMBER_ADMIN_ROLE

	# Duplicate e-mail -> 409 (DuplicateEntryError) for the existing-account path.
	if frappe.db.exists("User", {"email": email}):
		frappe.throw(f"An account already exists for {email}.", frappe.DuplicateEntryError)

	# Server-side re-verification; throws on not-found / invalid / unreachable.
	record = _verify_certificate(certificate_id)

	# Prefer authoritative register names for the organization and product.
	org_name = record["organization_name"] or organization_name
	product_name = record["system_name"] or system_name or "HMIS"
	product_release = release_version or record["system_version"] or "unspecified"

	# Registration only creates new organizations; members of an existing
	# organization are onboarded after sign-in by that organization's
	# single Vendor Administrator.
	if frappe.db.exists("HISI Vendor Organization", org_name):
		frappe.throw(
			"An account already exists for this organization. "
			"Ask the Vendor Administrator to onboard you after sign-in.",
			frappe.DuplicateEntryError,
		)

	try:
		frappe.db.savepoint("onboarding_register")

		# The user must exist before organization membership links to it.
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": full_name,
				"send_welcome_email": 0,
				"roles": [{"role": VENDOR_ROLE}],
			}
		).insert(ignore_permissions=True)
		update_password(email, password)

		# New organizations enter the payer workflow at its start status
		# (``in_review`` by default); the DHA certificate link is attached
		# after the certification reference is created below, so the admin
		# console shows them as pending from the moment of sign-up.
		org = frappe.get_doc(
			{
				"doctype": "HISI Vendor Organization",
				"organization_name": org_name,
				"payer_validation_status": _start_status_key(),
				"members": [{"user": email, "member_role": member_role}],
			}
		).insert(ignore_permissions=True)

		if frappe.db.exists("HISI HMIS Product", product_name):
			product = frappe.get_doc("HISI HMIS Product", product_name)
			if product.vendor != org.name:
				frappe.throw("This HMIS product already belongs to another vendor.", frappe.ValidationError)
			if product.release_version != product_release:
				product.release_version = product_release
				product.save(ignore_permissions=True)
		else:
			product = frappe.get_doc(
				{
					"doctype": "HISI HMIS Product",
					"vendor": org.name,
					"product_name": product_name,
					"release_version": product_release,
				}
			).insert(ignore_permissions=True)

		if frappe.db.exists("HISI DHA Certification Reference", record["certificate_number"]):
			cert_doc = frappe.get_doc("HISI DHA Certification Reference", record["certificate_number"])
			if cert_doc.vendor != org.name:
				frappe.throw("This certificate already belongs to another vendor.", frappe.ValidationError)
		else:
			cert_doc = frappe.get_doc(
				{
					"doctype": "HISI DHA Certification Reference",
					"certificate_id": record["certificate_number"],
					"vendor": org.name,
					"product": product.name,
					"certificate_status": STATUS_MAP.get(record["status"], "Unknown"),
					"issue_date": record["issued_date"],
					"expiry_date": record["expiry_date"],
					"verification_method": "Register lookup",
					"verified_on": frappe.utils.now_datetime(),
					"verified_by": "DHA public register",
					"verification_note": record["message"],
				}
			).insert(ignore_permissions=True)

		# The certification reference only exists now, and the organization's
		# dha_certificate_id is a Link field: Frappe validates links on insert,
		# so the link is attached on a follow-up save inside the same savepoint.
		org.dha_certificate_id = record["certificate_number"]
		org.save(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		frappe.db.rollback(save_point="onboarding_register")
		raise
	except Exception:
		frappe.db.rollback(save_point="onboarding_register")
		raise

	return {
		"email": email,
		"fullName": full_name,
		"organizationName": org.name,
		"memberRole": member_role,
		"userType": "vendor",
	}


@frappe.whitelist()
def add_organization_member(
	email: str,
	full_name: str,
	member_role: str = "Vendor Technical Lead",
	password: str = "",
) -> dict:
	"""Onboard a new member into the caller's organization.

	Session-gated: only the organization's Vendor Administrator may call it
	(each organization has exactly one). Creates the sign-in user (role
	``HISI Vendor``) when needed and appends the membership row; the new
	member is attached to the organization automatically on their next
	successful sign-in via ``hisi.api.account.get_my_profile``.
	"""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not authorized", frappe.PermissionError)

	membership = frappe.get_all(
		"HISI Organization Member",
		filters={"user": user, "member_role": MEMBER_ADMIN_ROLE},
		fields=["parent"],
		limit_page_length=1,
	)
	if not membership:
		frappe.throw("Only a Vendor Administrator can onboard members.", frappe.PermissionError)
	org_name = membership[0].parent

	email = (email or "").strip()
	full_name = (full_name or "").strip()
	member_role = (member_role or "").strip() or "Vendor Technical Lead"
	if not full_name:
		frappe.throw("Full name is required.", frappe.ValidationError)
	frappe.utils.validate_email_address(email, throw=True)
	if member_role not in MEMBER_ROLES:
		frappe.throw("Unknown member role.", frappe.ValidationError)
	# The organization already has its single Vendor Administrator.
	if member_role == MEMBER_ADMIN_ROLE:
		frappe.throw(
			"Each organization has only one Vendor Administrator. "
			"Invite the member as a technical or clinical lead instead.",
			frappe.ValidationError,
		)
	if password and len(password) < 8:
		frappe.throw("Password does not meet the minimum length.", frappe.ValidationError)

	org = frappe.get_doc("HISI Vendor Organization", org_name)
	if any(row.user == email for row in org.members):
		frappe.throw(f"{email} is already a member of this organization.", frappe.ValidationError)

	try:
		frappe.db.savepoint("onboarding_add_member")

		existing_user = frappe.db.get_value("User", {"email": email}, "name")
		if existing_user:
			member_user = frappe.get_doc("User", existing_user)
			if not any(row.role == VENDOR_ROLE for row in member_user.roles):
				member_user.append("roles", {"role": VENDOR_ROLE})
				member_user.save(ignore_permissions=True)
		else:
			member_user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": full_name,
					"send_welcome_email": 0,
					"roles": [{"role": VENDOR_ROLE}],
				}
			).insert(ignore_permissions=True)
		if password:
			update_password(email, password)

		org.append("members", {"user": email, "member_role": member_role})
		org.save(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point="onboarding_add_member")
		raise

	frappe.db.commit()
	return {
		"organizationName": org.name,
		"user": email,
		"fullName": full_name,
		"memberRole": member_role,
	}
