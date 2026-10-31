"""One-time migration: organizations become ERPNext Companies; membership
becomes Employee records.

- Each HISI Payer Organization / HISI Vendor Organization without a company
  gets one (company_name = the org's display name; an existing Company with
  the same name is reused).
- Every existing HISI Payer Organization Member / HISI Organization Member
  row gets an active Employee (user_id + company + designation).

Idempotent: safe to re-run.
"""
import frappe

from hisi.utils import upsert_org_employee


def ensure_company(display_name):
	row = frappe.get_all("Company", filters={"company_name": display_name}, fields=["name"], limit=1)
	if row:
		return row[0].name
	doc = frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": display_name,
			"country": "Kenya",
			"default_currency": "KES",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def execute():
	for row in frappe.get_all("HISI Payer Organization", fields=["name", "payer_name", "company"]):
		if not row["company"]:
			company = ensure_company(row.payer_name)
			frappe.db.set_value("HISI Payer Organization", row["name"], "company", company)
			print("company for payer org {} -> {}".format(row["name"], company))
	for row in frappe.get_all("HISI Vendor Organization", fields=["name", "organization_name", "company"]):
		if not row["company"]:
			company = ensure_company(row.organization_name)
			frappe.db.set_value("HISI Vendor Organization", row["name"], "company", company)
			print("company for vendor org {} -> {}".format(row["name"], company))

	for row in frappe.get_all("HISI Payer Organization Member", fields=["parent", "user"]):
		company = frappe.db.get_value("HISI Payer Organization", row["parent"], "company")
		if company:
			upsert_org_employee(row["user"], company, designation="HISI Insurance Surveillance")
	for row in frappe.get_all("HISI Organization Member", fields=["parent", "user", "member_role"]):
		company = frappe.db.get_value("HISI Vendor Organization", row["parent"], "company")
		if company:
			upsert_org_employee(row["user"], company, designation=row["member_role"] or None)

	frappe.db.commit()
	employees = frappe.db.count("Employee")
	companies = frappe.db.count("Company")
	print("done: {} companies, {} employees".format(companies, employees))
