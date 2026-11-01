"""Seed a fresh dispatched mandate for the officer so the findings flow can be
tested end-to-end (the previous mandate's session has since lapsed)."""
import json
import secrets
from datetime import timedelta

import frappe

src = frappe.get_all(
    "HISI Surveillance Mandate",
    filters={"mandate_no": "SURV-128A2F"},
    fields=["company", "facility", "assigned_officer", "radius_km", "location_policy"],
    limit=1,
)
if not src:
    raise SystemExit("Template mandate SURV-128A2F not found")
tpl = src[0]
officer = tpl["assigned_officer"]
now = frappe.utils.now_datetime()
mandate_no = "SURV-" + secrets.token_hex(3).upper()
short_code = secrets.token_hex(4).upper()
doc = frappe.get_doc(
    {
        "doctype": "HISI Surveillance Mandate",
        "mandate_no": mandate_no,
        "company": tpl["company"],
        "facility": tpl["facility"],
        "assigned_officer": officer,
        "reason": "Findings recording acceptance test (documents + screenshots + free text).",
        "valid_from": now - timedelta(minutes=5),
        "expires_at": now + timedelta(hours=2),
        "status": "dispatched",
        "short_code": short_code,
        "radius_km": tpl["radius_km"],
        "location_policy": tpl["location_policy"] or "flag",
        "scope_lines": [
            {"line_type": "patient", "patient_identifier": "CR9988776655443-1", "visit_id": "", "note": "Test scope line"},
        ],
    }
)
doc.insert(ignore_permissions=True)
frappe.db.commit()
print(json.dumps({"mandate_no": mandate_no, "short_code": short_code, "name": doc.name}))
