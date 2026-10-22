"""Facility Scope endpoints (hisi.api.facilities.*).

Manages facility deployment scope and readiness evaluations for HMIS systems.
"""

import frappe
from frappe import _

from hisi.api._common import clean, not_found, ok, require_user, require_vendor_actor
from hisi.api.validation import dha_track, portal_state
from hisi.constants import VENDOR_ROLES
from hisi.utils import get_user_orgs, is_platform_user


@frappe.whitelist(methods=["GET"])
def list_facilities() -> dict:
	"""List facility scopes visible to the user's organization or all if platform reviewer."""
	require_user()
	facilities = frappe.get_list(
		"HISI Facility Scope",
		fields=[
			"name",
			"vendor",
			"facility_name",
			"facility_code",
			"keph_level",
			"county",
			"hmis_product",
			"hmis_version",
			"dha_certificate",
			"evaluation_owner",
			"surveillance_status",
		],
		order_by="creation desc",
		limit=500,
	)

	items = []
	for f in facilities:
		# Check latest DHA certificate status
		dha_status = "Pending verification"
		if f.dha_certificate and frappe.db.exists("HISI DHA Certification Reference", f.dha_certificate):
			cert = frappe.get_doc("HISI DHA Certification Reference", f.dha_certificate)
			dha_status = dha_track(cert)

		# Check latest validation case state if any
		cases = frappe.get_list(
			"HISI SHA Validation Case",
			filters={"vendor": f.vendor, "product": f.hmis_product},
			fields=["state"],
			order_by="modified desc",
			limit=1,
		)
		sha_state = portal_state(cases[0].state) if cases else "Scope confirmation"

		items.append(
			{
				"id": f.name,
				"facilityName": f.facility_name,
				"facilityCode": f.facility_code,
				"kephLevel": f.keph_level,
				"county": f.county,
				"hmisProduct": f.hmis_product,
				"hmisVersion": f.hmis_version,
				"dhaCertificateId": f.dha_certificate,
				"dhaStatus": dha_status,
				"shaValidationState": sha_state,
				"evaluationOwner": f.evaluation_owner,
				"surveillanceStatus": f.surveillance_status or "Not connected",
			}
		)

	return ok({"facilities": items})


@frappe.whitelist(methods=["POST"])
def add_facility_scope(
	facility_name: str,
	facility_code: str,
	keph_level: str,
	county: str,
	hmis_product: str,
	hmis_version: str,
	dha_certificate_id: str,
	evaluation_owner: str,
) -> dict:
	"""Add a facility scope for deployment and validation tracking."""
	user = require_user()
	facility_name = clean(facility_name, 140)
	facility_code = clean(facility_code, 64)
	keph_level = clean(keph_level, 32)
	county = clean(county, 64)
	hmis_product = clean(hmis_product, 140)
	hmis_version = clean(hmis_version, 64)
	dha_certificate_id = clean(dha_certificate_id, 64)
	evaluation_owner = clean(evaluation_owner, 140)

	if not (
		facility_name and facility_code and keph_level and county and hmis_product and dha_certificate_id
	):
		frappe.throw(_("All facility fields are required."), frappe.ValidationError)

	cert_vendor = frappe.db.get_value("HISI DHA Certification Reference", dha_certificate_id, "vendor")
	if not cert_vendor or (not is_platform_user(user) and cert_vendor not in get_user_orgs(user)):
		not_found("DHA Certificate")

	# Check duplicate facility code within organization
	if frappe.db.exists("HISI Facility Scope", {"vendor": cert_vendor, "facility_code": facility_code}):
		frappe.throw(
			_("Facility with code {0} already registered.").format(facility_code), frappe.DuplicateEntryError
		)

	doc = frappe.get_doc(
		{
			"doctype": "HISI Facility Scope",
			"vendor": cert_vendor,
			"facility_name": facility_name,
			"facility_code": facility_code,
			"keph_level": keph_level,
			"county": county,
			"hmis_product": hmis_product,
			"hmis_version": hmis_version,
			"dha_certificate": dha_certificate_id,
			"evaluation_owner": evaluation_owner,
			"surveillance_status": "Not connected",
		}
	).insert(ignore_permissions=True)

	return ok({"facilityId": doc.name})
