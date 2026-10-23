"""Idempotent synthetic demo data for the HISI SHA validation vertical slice.

Run with:
    bench --site <site> execute hisi.seed.run_demo_seed

All records are fictional. This seed only creates records that do not exist
yet and never mutates existing ones.
"""

import frappe

CRITERIA = [
	("CC-01", "Pre-Certification Gap Identification", "Structured intake and SHA validation/testing before sign-off; all critical defects must be resolved before sign-off.", "Intake record, test plan, gap register", 1),
	("CC-02", "Uniform Benefits Matrix & Access Rules", "HIE/SHA benefit and access-rule fixtures and claim/preauthorization assertions.", "API assertions, benefit-rule response evidence", 1),
	("CC-03", "Standardized Patient Journey Capture", "Journey API checks, Visit ID propagation, clinical evidence and claim traceability.", "API journey trace, video evidence", 1),
	("CC-04", "User Capacity & Training", "Structured, validated in-person/hybrid training plan for relevant facility and hospital user roles; completion and reviewer evidence.", "Training plan, completion evidence", 0),
	("CC-05", "Minimum Integration Requirements", "API preflight, supported profiles, security, required integrations and data exchange.", "Preflight report, configuration evidence", 1),
	("CC-06", "Claims & Patient Journey Visibility", "Claim/notification correlation, status progression and facility/platform-level views.", "Visibility review, API correlation trace", 0),
	("CC-07", "Authorizations and Claims Tracking", "Authorization and preauthorization lifecycle, claim linkage, submission and response tracking.", "API lifecycle trace", 1),
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

	# Cases insert at the workflow baseline ("Linked to DHA record") and are
	# then jumped to their demo state with a direct write: replaying every
	# transition would need the actor roles the seed user does not hold.
	if not frappe.get_list("HISI SHA Validation Case", limit=1):
		case = frappe.get_doc(
			{
				"doctype": "HISI SHA Validation Case",
				"case_name": "HCV-2026-0001",
				"vendor": ORG,
				"product": PRODUCT,
				"dha_certificate": CERT,
				"checklist_version": CHECKLIST,
				"release_version": "4.2.1",
				"scope": "SHA Digital Claims workflow",
				"state": "Linked to DHA record",
				"current_owner": frappe.session.user,
				"next_action": "Start autonomous API checks",
			}
		).insert(ignore_if_duplicate=True)
		frappe.db.set_value("HISI SHA Validation Case", case.name, "state", "Ready to test")
		created.append("validation case 'HCV-2026-0001'")

	if not frappe.db.exists("HISI SHA Validation Case", "HCV-2026-0002"):
		case = frappe.get_doc(
			{
				"doctype": "HISI SHA Validation Case",
				"case_name": "HCV-2026-0002",
				"vendor": ORG,
				"product": PRODUCT,
				"dha_certificate": CERT,
				"checklist_version": CHECKLIST,
				"release_version": "4.1.0",
				"scope": "SHA outpatient claims workflow",
				"state": "Linked to DHA record",
				"current_owner": frappe.session.user,
				"next_action": "Resolve failed criterion before acknowledgement",
			}
		).insert(ignore_if_duplicate=True)
		frappe.db.set_value("HISI SHA Validation Case", case.name, "state", "Manual review")
		created.append("validation case 'HCV-2026-0002'")

	created.extend(_ensure_assessments("HCV-2026-0001", CHECKLIST, all_pass=True))
	created.extend(_ensure_assessments("HCV-2026-0002", CHECKLIST, all_pass=False))

	if created:
		frappe.db.commit()
		return created
	return ["demo data already present; nothing created"]


def _ensure_assessments(case_name: str, checklist: str, all_pass: bool) -> list[str]:
	"""Seed criterion assessments for a demo case when it has none.

	`all_pass=True` produces a case ready for platform acknowledgement;
	`all_pass=False` leaves one failed and one unassessed criterion so the
	administrator workspace has a not-ready case to show.
	"""
	if frappe.db.exists(
		"HISI Case Criterion Assessment", {"parenttype": "HISI SHA Validation Case", "parent": case_name}
	):
		return []

	criteria = frappe.get_all(
		"HISI Checklist Criterion",
		filters={"parenttype": "HISI Checklist Version", "parent": checklist},
		fields=["criterion_id", "title", "is_critical"],
		order_by="idx asc",
	)
	if not criteria:
		return []

	case = frappe.get_doc("HISI SHA Validation Case", case_name)
	for index, criterion in enumerate(criteria):
		if all_pass:
			assessment = "Pass"
			rationale = "Seeded passing assessment for demonstration."
		elif index == len(criteria) - 1:
			assessment = "Not assessed"
			rationale = ""
		elif index == 0:
			assessment = "Fail"
			rationale = "Seeded failing assessment so acknowledgement stays blocked."
		else:
			assessment = "Pass"
			rationale = "Seeded passing assessment for demonstration."
		case.append(
			"criteria_assessments",
			{
				"criterion_id": criterion.criterion_id,
				"criterion_title": criterion.title,
				"is_critical": criterion.is_critical,
				"assessment": assessment,
				"rationale": rationale,
				"assessed_by": frappe.session.user if assessment != "Not assessed" else None,
				"assessed_on": frappe.utils.now() if assessment != "Not assessed" else None,
			},
		)
	case.save()
	return [f"{len(criteria)} criterion assessments on {case_name!r}"]


VENDOR_USER = "vendor.test@example.com"
VENDOR_ORG = "Test Vendor Ltd"
VENDOR_PASSWORD = "root"


def run_vendor_user_seed() -> list[str]:
	"""Idempotent vendor sign-in fixture for local verification.

	Creates the vendor user (role `HISI Vendor`, password `root`) and links
	them to a vendor organization as `Vendor Administrator`, so
	`hisi.api.account.get_my_profile` can be exercised for the vendor path.

	Run with:
	    bench --site <site> execute hisi.seed.run_vendor_user_seed
	"""
	created = []

	if not frappe.db.exists("User", VENDOR_USER):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": VENDOR_USER,
				"first_name": "Vendy",
				"last_name": "Tester",
				"full_name": "Vendy Tester",
				"send_welcome_email": 0,
				"roles": [{"role": "HISI Vendor"}],
			}
		).insert(ignore_if_duplicate=True)
		created.append(f"user {VENDOR_USER!r}")

	from frappe.utils.password import update_password

	update_password(VENDOR_USER, VENDOR_PASSWORD)

	user_doc = frappe.get_doc("User", VENDOR_USER)
	if not any(row.role == "HISI Vendor" for row in user_doc.roles):
		user_doc.append("roles", {"role": "HISI Vendor"})
		user_doc.save(ignore_permissions=True)
		created.append("role 'HISI Vendor'")

	if not frappe.db.exists("HISI Vendor Organization", VENDOR_ORG):
		org = frappe.get_doc(
			{
				"doctype": "HISI Vendor Organization",
				"organization_name": VENDOR_ORG,
				"members": [{"user": VENDOR_USER, "member_role": "Vendor Administrator"}],
			}
		).insert(ignore_if_duplicate=True)
		created.append(f"vendor organization {org.name!r}")
	else:
		org = frappe.get_doc("HISI Vendor Organization", VENDOR_ORG)
		if not any(row.user == VENDOR_USER for row in org.members):
			org.append("members", {"user": VENDOR_USER, "member_role": "Vendor Administrator"})
			org.save(ignore_permissions=True)
			created.append(f"membership for {VENDOR_USER!r}")

	if created:
		frappe.db.commit()
		return created
	return ["vendor fixture already present; nothing created"]
