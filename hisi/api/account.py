"""Session profile for the HISI frontend.

Single source of truth for the signed-in user's workspace identity:
`userType` (admin | vendor | unassigned), the active display `role`, and the
`availableRoles` the workspace may offer for switching.

Separation is role-based only:

- administrator: the user holds the ``System Manager`` role;
- vendor: the user holds the ``HISI Vendor`` role (organization membership,
  when present, supplies the organization name and display role);
- anything else: ``unassigned`` (workspace sends them to /access-denied).

On sign-in a vendor is already attached to their organization: the profile
derives the organization name, website, and — when the organization holds a
confirmed DHA certificate — links it automatically (``linkedCertificateId``)
so the evaluation workspace needs no manual linking step. Vendor
administrators additionally get the member-management capability
(``canManageMembers``); each organization has exactly one Vendor
Administrator.

The browser only ever renders what this method derives from the session —
it never decides access.
"""

import frappe

# The two roles that define the admin / vendor separation.
ADMIN_ROLE = "System Manager"
VENDOR_ROLE = "HISI Vendor"

# Display role the administrator workspace shows.
ADMIN_DISPLAY_ROLE = "HISI Administrator"

# The single member role that may manage its organization.
MEMBER_ADMIN_ROLE = "Vendor Administrator"

# `HISI Organization Member.member_role` -> workspace display role.
MEMBER_ROLE_MAP = {
	"Vendor Administrator": "HMIS Vendor Administrator",
	"Vendor Technical Lead": "Vendor Technical Lead",
	"Vendor Clinical / Operations Lead": "Vendor Clinical / Operations Lead",
}

DEFAULT_VENDOR_DISPLAY_ROLE = "HMIS Vendor Administrator"

# Certificate statuses that count as confirmed for automatic linking.
CONFIRMED_CERTIFICATE_STATUSES = ("Valid", "Conditional")

WEBSITE_MAX_LENGTH = 256


def _vendor_profile(user: str) -> dict:
	"""Vendor identity from the HISI Vendor role, enriched with organization
	membership when the user belongs to a HISI Vendor Organization."""
	memberships = frappe.get_all(
		"HISI Organization Member",
		filters={"user": user},
		fields=["parent", "member_role"],
		order_by="parent asc",
	)

	org_names = sorted({row.parent for row in memberships})
	display_roles = [MEMBER_ROLE_MAP.get(row.member_role) for row in memberships]
	known_roles = [role for role in display_roles if role]
	member_role = memberships[0].member_role if memberships else ""
	website, certificate_id, system_name = _organization_context(org_names[0] if org_names else "")

	return {
		"userType": "vendor",
		"role": known_roles[0] if known_roles else DEFAULT_VENDOR_DISPLAY_ROLE,
		# Distinct roles this member may switch to, in canonical order.
		"availableRoles": list(dict.fromkeys(known_roles)) or [DEFAULT_VENDOR_DISPLAY_ROLE],
		"organizationName": _organization_name(org_names[0]) if org_names else "",
		"organizationWebsite": website,
		"memberRole": member_role,
		# Only the organization's single Vendor Administrator manages members.
		"canManageMembers": member_role == MEMBER_ADMIN_ROLE,
		# Confirmed DHA certificate linked automatically on sign-in.
		"linkedCertificateId": certificate_id,
		"linkedSystemName": system_name,
	}


def _organization_name(org: str) -> str:
	return frappe.db.get_value("HISI Vendor Organization", org, "organization_name") or org


def _organization_context(org: str) -> tuple[str, str, str]:
	"""Website plus the organization's confirmed DHA certificate, if any.

	The certificate is taken from the organization's certification references
	whose status is confirmed (Valid or Conditional), most recently verified
	first — sign-in links it automatically with no manual step.
	"""
	if not org:
		return "", "", ""
	website = frappe.db.get_value("HISI Vendor Organization", org, "website") or ""
	certificates = frappe.get_all(
		"HISI DHA Certification Reference",
		filters={"vendor": org, "certificate_status": ["in", list(CONFIRMED_CERTIFICATE_STATUSES)]},
		fields=["certificate_id", "product"],
		order_by="verified_on desc, modified desc",
		limit_page_length=1,
	)
	if not certificates:
		return website, "", ""
	system_name = frappe.db.get_value("HISI HMIS Product", certificates[0].product, "product_name") or ""
	return website, certificates[0].certificate_id, system_name


@frappe.whitelist()
def get_my_profile() -> dict:
	"""Return the signed-in user's profile for the HISI workspace.

	Requires an authenticated Frappe session (sid); Guest is rejected here as
	well as by the whitelist itself (defence in depth).
	"""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not authorized", frappe.PermissionError)

	roles = list(frappe.get_roles(user))
	user_row = (
		frappe.db.get_value(
			"User", user, ["full_name", "email", "phone", "gender", "last_login"], as_dict=True
		)
		or frappe._dict()
	)
	last_login = user_row.get("last_login")

	profile = {
		"email": user_row.get("email") or user,
		"fullName": user_row.get("full_name") or user,
		"phoneNumber": user_row.get("phone") or "",
		"gender": user_row.get("gender") or "",
		"signInMethod": "Organization sign-in",
		# User.last_login is a Read Only string ("YYYY-MM-DD HH:MM:SS").
		"lastSignIn": str(last_login)[:16] if last_login else "",
		"roles": roles,
		"organizationName": "",
	}

	if ADMIN_ROLE in roles:
		return {
			**profile,
			"userType": "admin",
			"role": ADMIN_DISPLAY_ROLE,
			"availableRoles": [ADMIN_DISPLAY_ROLE],
		}

	if VENDOR_ROLE in roles:
		return {**profile, **_vendor_profile(user)}

	# Authenticated but holding neither role: the workspace sends these
	# users to /access-denied until an administrator links them.
	return {**profile, "userType": "unassigned", "role": None, "availableRoles": []}


def _require_vendor_administrator() -> tuple[str, str]:
	"""The caller's organization and user, when the caller is its (single) Vendor Administrator."""
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
		frappe.throw("Only a Vendor Administrator can manage the organization.", frappe.PermissionError)
	return membership[0].parent, user


@frappe.whitelist()
def update_organization_website(website: str) -> dict:
	"""Set the organization website; only the organization's Vendor Administrator."""
	org, user = _require_vendor_administrator()
	url = (website or "").strip()[:WEBSITE_MAX_LENGTH]
	if url and not url.startswith(("http://", "https://")):
		frappe.throw("Website must start with http:// or https://.", frappe.ValidationError)
	doc = frappe.get_doc("HISI Vendor Organization", org)
	if doc.website != url:
		doc.website = url
		doc.save(ignore_permissions=True)
		frappe.db.commit()
	return {
		"organizationName": doc.organization_name,
		"organizationWebsite": doc.website or "",
		"updatedBy": user,
	}


@frappe.whitelist()
def list_organization_members() -> dict:
	"""Members of the caller's organization, with display roles.

	Vendor members see their own organization; platform users without a
	membership simply see no organization.
	"""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not authorized", frappe.PermissionError)

	memberships = frappe.get_all(
		"HISI Organization Member",
		filters={"user": user},
		fields=["parent"],
		limit_page_length=1,
	)
	if not memberships:
		if ADMIN_ROLE in frappe.get_roles(user):
			return {"organizationName": "", "members": []}
		frappe.throw("Not authorized", frappe.PermissionError)

	org = memberships[0].parent
	rows = frappe.get_all(
		"HISI Organization Member",
		filters={"parent": org},
		fields=["user", "member_role"],
		order_by="idx asc",
	)
	full_names: dict[str, str] = {}
	if rows:
		full_names = {
			row.name: row.full_name
			for row in frappe.get_all(
				"User",
				filters={"name": ["in", sorted({r.user for r in rows})]},
				fields=["name", "full_name"],
			)
		}
	return {
		"organizationName": _organization_name(org),
		"organizationWebsite": frappe.db.get_value("HISI Vendor Organization", org, "website") or "",
		"members": [
			{
				"user": row.user,
				"fullName": full_names.get(row.user) or row.user,
				"memberRole": row.member_role or "",
				"displayRole": MEMBER_ROLE_MAP.get(row.member_role, row.member_role or ""),
			}
			for row in rows
		],
	}
