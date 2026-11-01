"""Retire stale test data so the officer queue shows a single clean mandate."""
import frappe

frappe.db.set_value("HISI Surveillance Mandate", None, "status", "expired", {"mandate_no": "SURV-128A2F"})
frappe.db.set_value("HISI Surveillance Session", "SS-B486BC", "status", "ended")
frappe.db.commit()
print("cleaned")
