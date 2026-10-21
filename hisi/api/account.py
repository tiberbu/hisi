"""User profile endpoints (hisi.api.account.*).

Returns profile and organization scoping details for the authenticated user session.
"""

import frappe

from hisi.api._common import fmt_dt, ok, require_user
from hisi.constants import ROLE_PRIORITY
from hisi.utils import get_user_orgs, is_platform_user, is_reviewer


@frappe.whitelist(methods=["GET"])
def get_my_profile() -> dict:
	"""Return the current logged-in user's HISI profile."""
	user = require_user()
	user_doc = frappe.get_doc("User", user)
	roles = set(frappe.get_roles(user))

	# Determine primary user role
	primary_role = "HMIS Vendor Administrator"
	for r in ROLE_PRIORITY:
		if r in roles:
			primary_role = r
			break
	if "System Manager" in roles and primary_role == "HMIS Vendor Administrator":
		primary_role = "HISI Administrator"

	# Get linked vendor organization
	orgs = get_user_orgs(user)
	org_name = (
		orgs[0]
		if orgs
		else ("HISI Platform Review Team" if is_platform_user(user) else "Authorized Organization")
	)

	# Find any linked DHA certificate or HMIS system
	linked_cert = ""
	linked_system = ""
	if orgs:
		certs = frappe.get_all(
			"HISI DHA Certification Reference",
			filters={"vendor": ["in", orgs]},
			fields=["certificate_id", "product"],
			order_by="creation desc",
			limit=1,
		)
		if certs:
			linked_cert = certs[0].certificate_id
			if certs[0].product:
				linked_system = (
					frappe.db.get_value("HISI HMIS Product", certs[0].product, "product_name") or ""
				)

	# Check sign-in method
	sign_in_method = "Password"
	if frappe.db.exists("DocType", "Social Login Key"):
		# Check if User has social logins linked
		socials = frappe.get_all("User Social Login", filters={"parent": user}, pluck="provider")
		if socials:
			sign_in_method = f"OAuth ({', '.join(s.title() for s in socials)})"

	return ok(
		{
			"fullName": user_doc.full_name or user_doc.first_name or user.split("@")[0],
			"email": user_doc.email,
			"organizationName": org_name,
			"role": primary_role,
			"signInMethod": sign_in_method,
			"linkedCertificateId": linked_cert,
			"linkedSystemName": linked_system,
			"lastSignIn": fmt_dt(user_doc.last_login),
		}
	)
