"""Server-side permission model for HISI domain documents.

Registered in hooks.py via the `has_permission` and
`permission_query_conditions` hooks. These are the only row-level access
controls that Frappe actually enforces:

- `get_permission_query_conditions` scopes list queries (get_list/db_query).
- `has_permission` gates access to a specific document (get_doc and friends).

Rules:
- Guest: no access.
- Platform roles (System Manager, HISI Administrator, SHA reviewers, Auditor /
  Observer): read across organizations; write is still governed by DocType
  role permissions (only System Manager / HISI Administrator in Desk).
- Vendor users: read-only, restricted to documents of organizations where they
  are an *active* member (HISI Vendor Organization > Members). Portal
  mutations go through hisi.api.* which performs explicit, role-aware checks
  (CWE-639 / CWE-285).
"""

import frappe

from hisi.utils import get_user_orgs, is_platform_user

# DocType -> field holding the vendor organization (the org document uses its own name).
_ORG_FIELD = {
	"HISI Vendor Organization": "name",
	"HISI HMIS Product": "vendor",
	"HISI DHA Certification Reference": "vendor",
	"HISI SHA Validation Case": "vendor",
	"HISI Test Run": "vendor",
	"HISI Evidence Item": "vendor",
	"HISI Finding": "vendor",
	"HISI Facility Scope": "vendor",
	"HISI Integration Profile": "vendor",
	"HISI SHA Disposition": "vendor",
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
	if ptype not in ("read", "print", "report"):
		return False
	org_field = _ORG_FIELD.get(doc.doctype)
	if not org_field:
		return True
	return getattr(doc, org_field) in get_user_orgs(user)


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

	orgs = get_user_orgs(user)
	if not orgs:
		return "0=1"
	in_clause = ", ".join(frappe.db.escape(org) for org in orgs)
	return f"`tab{doctype}`.`{org_field}` in ({in_clause})"
