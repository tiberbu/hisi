"""Idempotent provisioning for the HISI app (roles + published reference checklist)."""

import frappe

from hisi.constants import ROLE_DEFINITIONS, criteria_reference

CHECKLIST_VERSION = "SHA-HMIS-2026.10"


def after_install():
	provision()


def after_migrate():
	provision()


def provision():
	ensure_roles()
	ensure_reference_checklist()


def ensure_roles():
	for role_name, desk_access in ROLE_DEFINITIONS.items():
		if frappe.db.exists("Role", role_name):
			continue
		frappe.get_doc(
			{"doctype": "Role", "role_name": role_name, "desk_access": desk_access, "is_custom": 0}
		).insert(ignore_permissions=True)


def ensure_reference_checklist():
	"""Publish the canonical SHA criteria (CC-01..CC-07) once; never mutate an existing version."""
	if frappe.db.exists("HISI Checklist Version", CHECKLIST_VERSION):
		return
	frappe.get_doc(
		{
			"doctype": "HISI Checklist Version",
			"version": CHECKLIST_VERSION,
			"effective_on": "2026-10-01",
			"published": 1,
			"release_notes": "Enhanced SHA claims validation checklist (CC-01 to CC-07) with fraud-control traceability.",
			"criteria": [
				{
					"criterion_id": c["id"],
					"title": c["title"],
					"statement": c["coverage"],
					"evidence_options": c["evidenceMode"],
					"is_critical": 1 if c["mandatoryBlocker"] else 0,
				}
				for c in criteria_reference()
			],
		}
	).insert(ignore_permissions=True)
