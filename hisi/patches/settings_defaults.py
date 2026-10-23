"""Seed HISI Settings defaults.

Singles are read programmatically from ``tabSingles``; field defaults in the
DocType JSON only apply to the Desk form, so materialise the DHA register
base URL row at migrate time for every site.
"""

import frappe

DEFAULT_DHA_VERIFY_URL = "https://service.dha.go.ke/api/public/verify"


def execute():
	if not frappe.db.exists("DocType", "HISI Settings"):
		return
	if not (frappe.db.get_value("HISI Settings", "HISI Settings", "dha_verify_url") or "").strip():
		frappe.db.set_value("HISI Settings", "HISI Settings", "dha_verify_url", DEFAULT_DHA_VERIFY_URL)
	frappe.db.commit()
