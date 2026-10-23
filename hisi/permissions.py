"""Server-side permission model for HISI domain documents.

Registered in hooks.py via the `has_permission` and
`permission_query_conditions` hooks. These are the only row-level access
controls that Frappe actually enforces:

- `get_permission_query_conditions` scopes list queries (get_list/db_query).
- `has_permission` gates access to a specific document (get_doc and friends).

Rules:
- Guest: no access.
- Platform roles (System Manager, HISI Reviewer): full access.
- Vendor users: restricted to documents of their own organization (membership
  recorded on HISI Vendor Organization > Members). Read everywhere allowed,
  plus create/write on the SHA validation workflow documents (case and test
  run) — still scoped to their own organization and further limited by the
  DocType role permissions. Acknowledgement fields are guarded in the case
  DocType's validate().
"""

import frappe

from hisi.utils import is_org_member, is_platform_user

# DocType -> field holding the vendor organization (the org document uses its own name).
_ORG_FIELD = {
	"HISI Vendor Organization": "name",
	"HISI HMIS Product": "vendor",
	"HISI DHA Certification Reference": "vendor",
	"HISI SHA Validation Case": "vendor",
	"HISI Test Run": "vendor",
}

ORG_SCOPED_DOCTYPES = tuple(_ORG_FIELD)


def has_permission(doc, ptype, user=None):
	"""Access to a specific document. May only deny; role permissions still apply."""
	if not user:
		user = frappe.session.user
	if user == "Guest":
		return False
	if is_platform_user(user):
		return True
	# Vendors never delete or manage sharing; read/write/create fall through
	# to the organization check below (role permissions gate the ptype too).
	if ptype not in ("read", "write", "create"):
		return False
	org_field = _ORG_FIELD.get(doc.doctype)
	if not org_field:
		return ptype == "read"
	return is_org_member(getattr(doc, org_field, None) or "", user)


def get_permission_query_conditions(user=None, doctype: str | None = None):
	"""SQL conditions scoping list queries to the user's organization."""
	if not user:
		user = frappe.session.user
	if user == "Guest":
		return "0=1"
	if is_platform_user(user):
		return None

	doctype = doctype or "HISI SHA Validation Case"
	org_field = _ORG_FIELD.get(doctype)
	if not org_field:
		return None

	orgs = frappe.get_all("HISI Organization Member", filters={"user": user}, pluck="parent")
	if not orgs:
		return "0=1"
	in_clause = ", ".join(frappe.db.escape(org) for org in orgs)
	return f"`tab{doctype}`.`{org_field}` in ({in_clause})"
