import frappe

from hisi.checklist import assessments_of, criteria_of


@frappe.whitelist(allow_guest=False)
def get_validation_case_view(case: str | None = None) -> dict:
	if frappe.session.user == "Guest":
		frappe.throw("Not authorized", frappe.PermissionError)

	if case:
		doc = frappe.get_doc("HISI SHA Validation Case", case)
		doc.check_permission("read")
	else:
		first = frappe.get_list(
			"HISI SHA Validation Case",
			filters={"docstatus": ["<", 2]},
			fields=["name"],
			order_by="name asc",
			limit=1,
		)
		if not first:
			return {"case": None, "vendor": None, "product": None, "dha_certificate": None, "checklist": None, "criteria": []}
		doc = frappe.get_doc("HISI SHA Validation Case", first[0].name)

	vendor = frappe.get_doc("HISI Vendor Organization", doc.vendor)
	vendor.check_permission("read")
	product = frappe.get_doc("HISI HMIS Product", doc.product)
	product.check_permission("read")
	dha = frappe.get_doc("HISI DHA Certification Reference", doc.dha_certificate)
	dha.check_permission("read")
	checklist = frappe.get_doc("HISI Checklist Version", doc.checklist_version)
	checklist.check_permission("read")

	assessment_by_id = assessments_of(doc.name)
	criteria = []
	for crit in criteria_of(doc.checklist_version):
		row = assessment_by_id.get(crit.criterion_id)
		criteria.append(
			{
				"criterion_id": crit.criterion_id,
				"title": crit.title,
				"is_critical": bool(crit.is_critical),
				"assessment": row.assessment if row else "Not assessed",
				"assessed_by": row.assessed_by if row else None,
				"assessed_on": str(row.assessed_on) if row and row.assessed_on else None,
				"rationale": row.rationale if row else None,
			}
		)

	return {
		"case": {
			"name": doc.name,
			"state": doc.state,
			"scope": doc.scope,
			"current_owner": doc.current_owner,
			"next_action": doc.next_action,
			"release_version": doc.release_version,
			"modified": str(doc.modified) if doc.modified else None,
		},
		"vendor": {"name": vendor.name, "organization_name": vendor.organization_name},
		"product": {
			"name": product.name,
			"product_name": product.product_name,
			"release_version": product.release_version,
		},
		"dha_certificate": {
			"name": dha.name,
			"certificate_id": dha.certificate_id,
			"certificate_status": dha.certificate_status,
			"verified_on": str(dha.verified_on) if dha.verified_on else None,
			"verified_by": dha.verified_by,
			"verification_method": dha.verification_method,
		},
		"checklist": {"name": checklist.name, "version": checklist.version, "published": bool(checklist.published)},
		"criteria": criteria,
	}
