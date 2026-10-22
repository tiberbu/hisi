"""Onboarding and guest verification API (hisi.api.onboarding.*).

Used by the HISI registration and onboarding flow.
- verify_dha_certificate: Checks DHA certificate status and returns public details.
- register_vendor_account: Onboards a new vendor user linked to an organization and DHA certificate.
"""

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime, today

from hisi.api._common import clean, fmt_date, fmt_dt, ok
from hisi.constants import DHA_STATUS_TO_PORTAL, MEMBER_ROLE_TO_ROLE, SIGNUP_ROLE_KEYS


@frappe.whitelist(allow_guest=True, methods=["POST"])
def verify_dha_certificate(
	certificate_id: str,
	system_name: str | None = None,
	vendor_name: str | None = None,
) -> dict:
	"""Public lookup to verify a DHA certificate reference during onboarding."""
	certificate_id = clean(certificate_id, 64)
	if not certificate_id:
		frappe.throw(_("DHA Certificate ID is required."), frappe.ValidationError)

	if not frappe.db.exists("HISI DHA Certification Reference", certificate_id):
		raise frappe.DoesNotExistError(_("Certificate not found on DHA register."))

	cert = frappe.get_doc("HISI DHA Certification Reference", certificate_id)
	product = frappe.get_doc("HISI HMIS Product", cert.product) if cert.product else None
	org = frappe.get_doc("HISI Vendor Organization", cert.vendor) if cert.vendor else None

	if system_name:
		sys_clean = clean(system_name, 140).casefold()
		if product and product.product_name.casefold() != sys_clean:
			raise frappe.DoesNotExistError(_("System name does not match the certificate record."))

	dha_status = DHA_STATUS_TO_PORTAL.get(cert.certificate_status or "Unknown", "Pending verification")
	if cert.expiry_date and getdate(cert.expiry_date) < getdate(today()):
		dha_status = "Expired"

	return ok(
		{
			"certificateId": cert.certificate_id,
			"systemName": product.product_name if product else "",
			"vendorName": org.organization_name if org else "",
			"dhaStatus": dha_status,
			"issueDate": fmt_date(cert.issue_date),
			"expiryDate": fmt_date(cert.expiry_date),
			"verifiedAt": fmt_dt(cert.verified_on),
			"scopeType": cert.get("scope_type") or "First-round DHA HMIS Certification (Published)",
		}
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def register_vendor_account(
	certificate_id: str,
	system_name: str,
	release_version: str,
	organization_name: str,
	full_name: str,
	email: str,
	password: str,
	role: str | None = "vendor_admin",
) -> dict:
	"""Guest registration for a vendor user linked to a DHA certificate and vendor organization."""
	certificate_id = clean(certificate_id, 64)
	system_name = clean(system_name, 140)
	release_version = clean(release_version, 64)
	organization_name = clean(organization_name, 140)
	full_name = clean(full_name, 140)
	email = clean(email, 254).lower()

	if not (certificate_id and system_name and organization_name and full_name and email and password):
		frappe.throw(_("All fields are required for registration."), frappe.ValidationError)

	if frappe.db.exists("User", email):
		# 409 Conflict
		raise frappe.DuplicateEntryError(_("An account with this email already exists."))

	# Verify certificate reference exists
	if not frappe.db.exists("HISI DHA Certification Reference", certificate_id):
		raise frappe.DoesNotExistError(_("DHA Certificate ID not found."))

	cert = frappe.get_doc("HISI DHA Certification Reference", certificate_id)

	# Check or create vendor organization
	vendor_doc = None
	if frappe.db.exists("HISI Vendor Organization", organization_name):
		vendor_doc = frappe.get_doc("HISI Vendor Organization", organization_name)
	elif cert.vendor and frappe.db.exists("HISI Vendor Organization", cert.vendor):
		vendor_doc = frappe.get_doc("HISI Vendor Organization", cert.vendor)
	else:
		vendor_doc = frappe.get_doc(
			{
				"doctype": "HISI Vendor Organization",
				"organization_name": organization_name,
				"status": "Active",
			}
		).insert(ignore_permissions=True)

	# Check or create product
	if not frappe.db.exists("HISI HMIS Product", system_name):
		frappe.get_doc(
			{
				"doctype": "HISI HMIS Product",
				"product_name": system_name,
				"vendor": vendor_doc.name,
				"release_version": release_version,
			}
		).insert(ignore_permissions=True)

	# Split full name
	names = full_name.split(None, 1)
	first_name = names[0]
	last_name = names[1] if len(names) > 1 else ""

	# Map portal role key to Org member role and Frappe system role
	member_role_label = SIGNUP_ROLE_KEYS.get(role, "Vendor Administrator")
	frappe_role = MEMBER_ROLE_TO_ROLE.get(member_role_label, "HMIS Vendor Administrator")

	# Create Frappe User
	user_doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": first_name,
			"last_name": last_name,
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "Website User",
			"roles": [{"role": frappe_role}, {"role": "HISI Vendor"}],
		}
	)
	user_doc.flags.no_welcome_mail = True
	user_doc.insert(ignore_permissions=True)

	# Set password securely
	from frappe.utils.password import update_password

	update_password(email, password, logout_all_sessions=0)

	# Add to organization members
	vendor_doc.append(
		"members",
		{
			"user": email,
			"member_role": member_role_label,
			"status": "Active",
		},
	)
	vendor_doc.save(ignore_permissions=True)

	return ok(
		{
			"email": email,
			"organization": vendor_doc.name,
			"status": "registered",
		}
	)
