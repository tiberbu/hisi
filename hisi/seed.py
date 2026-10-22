"""Idempotent synthetic demo data for the HISI SHA validation vertical slice.

Run with:
    bench --site <site> execute hisi.seed.run_demo_seed

All records are fictional. This seed only creates records that do not exist
yet and never mutates existing ones.
"""

import frappe

CRITERIA = [
	(
		"CC-01",
		"Pre-Certification Gap Identification",
		"Structured intake and SHA validation/testing before sign-off; all critical defects must be resolved before sign-off.",
		"Intake record, test plan, gap register",
		1,
	),
	(
		"CC-02",
		"Uniform Benefits Matrix & Access Rules",
		"HIE/SHA benefit and access-rule fixtures and claim/preauthorization assertions.",
		"API assertions, benefit-rule response evidence",
		1,
	),
	(
		"CC-03",
		"Standardized Patient Journey Capture",
		"Journey API checks, Visit ID propagation, clinical evidence and claim traceability.",
		"API journey trace, video evidence",
		1,
	),
	(
		"CC-04",
		"User Capacity & Training",
		"Structured, validated in-person/hybrid training plan for relevant facility and hospital user roles; completion and reviewer evidence.",
		"Training plan, completion evidence",
		0,
	),
	(
		"CC-05",
		"Minimum Integration Requirements",
		"API preflight, supported profiles, security, required integrations and data exchange.",
		"Preflight report, configuration evidence",
		1,
	),
	(
		"CC-06",
		"Claims & Patient Journey Visibility",
		"Claim/notification correlation, status progression and facility/platform-level views.",
		"Visibility review, API correlation trace",
		0,
	),
	(
		"CC-07",
		"Authorizations and Claims Tracking",
		"Authorization and preauthorization lifecycle, claim linkage, submission and response tracking.",
		"API lifecycle trace",
		1,
	),
]


def run_demo_seed() -> list[str]:
	created = []

	ORG = "Demo Health Systems"
	if not frappe.db.exists("HISI Vendor Organization", ORG):
		org = frappe.get_doc(
			{
				"doctype": "HISI Vendor Organization",
				"organization_name": ORG,
				"website": "https://example.org/demo-health",
				"members": [{"user": frappe.session.user, "member_role": "Vendor Administrator"}],
			}
		).insert(ignore_if_duplicate=True)
		created.append(f"vendor organization {org.name!r}")

	PRODUCT = "DemoHIS"
	if not frappe.db.exists("HISI HMIS Product", PRODUCT):
		product = frappe.get_doc(
			{
				"doctype": "HISI HMIS Product",
				"vendor": ORG,
				"product_name": PRODUCT,
				"release_version": "4.2.1",
				"platform_notes": "Fictional demonstration release used for local and CI data.",
			}
		).insert(ignore_if_duplicate=True)
		created.append(f"product {product.name!r}")

	CERT = "DHA-2026-00482"
	if not frappe.db.exists("HISI DHA Certification Reference", CERT):
		cert = frappe.get_doc(
			{
				"doctype": "HISI DHA Certification Reference",
				"certificate_id": CERT,
				"vendor": ORG,
				"product": PRODUCT,
				"certificate_status": "Valid",
				"verification_method": "Manual check",
				"verified_on": frappe.utils.now(),
				"verified_by": frappe.session.user,
				"verification_note": "Fictional certificate record; no live DHA register lookup is connected.",
			}
		).insert(ignore_if_duplicate=True)
		created.append(f"dha certification reference {cert.name!r}")

	CHECKLIST = "1.0"
	if not frappe.db.exists("HISI Checklist Version", CHECKLIST):
		checklist = frappe.get_doc(
			{
				"doctype": "HISI Checklist Version",
				"version": CHECKLIST,
				"effective_on": frappe.utils.today(),
				"published": 1,
				"release_notes": "First published release of the SHA claims criteria (CC-01 to CC-07).",
				"criteria": [
					{
						"criterion_id": criterion_id,
						"title": title,
						"statement": statement,
						"evidence_options": evidence,
						"is_critical": critical,
					}
					for criterion_id, title, statement, evidence, critical in CRITERIA
				],
			}
		).insert(ignore_if_duplicate=True)
		created.append(f"checklist version {checklist.name!r}")

	if not frappe.get_list("HISI SHA Validation Case", limit=1):
		frappe.get_doc(
			{
				"doctype": "HISI SHA Validation Case",
				"case_name": "HCV-2026-0001",
				"vendor": ORG,
				"product": PRODUCT,
				"dha_certificate": CERT,
				"checklist_version": CHECKLIST,
				"release_version": "4.2.1",
				"scope": "SHA Digital Claims workflow",
				"state": "Ready to test",
				"current_owner": frappe.session.user,
				"next_action": "Start autonomous API checks",
			}
		).insert(ignore_if_duplicate=True)
		created.append("validation case 'HCV-2026-0001'")

	if created:
		frappe.db.commit()
		return created
	return ["demo data already present; nothing created"]
