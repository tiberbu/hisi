"""Interoperability integration profiles API (hisi.api.interoperability.*).

Configures and tests endpoints for FHIR, SHR Visit ID, HIE Benefit Query, and auth schemes.
"""

import frappe
from frappe import _

from hisi.api._common import clean, fmt_dt, not_found, ok, require_user, require_vendor_actor
from hisi.constants import VENDOR_ROLES
from hisi.engine.net import normalize_sandbox_url, probe_fhir_metadata
from hisi.utils import get_user_orgs, is_platform_user


@frappe.whitelist(methods=["GET"])
def get_integration_profiles() -> dict:
	"""List integration profiles configured for HMIS test environments."""
	require_user()
	profiles = frappe.get_list(
		"HISI Integration Profile",
		fields=[
			"name",
			"vendor",
			"system_name",
			"release_version",
			"sandbox_base_url",
			"fhir_profile",
			"shr_visit_id_enabled",
			"hie_benefit_query_enabled",
			"auth_mode",
			"preflight_status",
			"last_verified",
		],
		order_by="creation desc",
		limit=100,
	)

	items = [
		{
			"id": p.name,
			"systemName": p.system_name,
			"releaseVersion": p.release_version,
			"sandboxBaseUrl": p.sandbox_base_url,
			"fhirProfile": p.fhir_profile,
			"shrVisitIdEnabled": bool(p.shr_visit_id_enabled),
			"hieBenefitQueryEnabled": bool(p.hie_benefit_query_enabled),
			"authMode": p.auth_mode,
			"preflightStatus": p.preflight_status or "Not connected",
			"lastVerified": fmt_dt(p.last_verified),
		}
		for p in profiles
	]

	return ok({"profiles": items})


@frappe.whitelist(methods=["POST"])
def save_integration_profile(
	system_name: str,
	release_version: str,
	sandbox_base_url: str,
	fhir_profile: str,
	shr_visit_id_enabled: bool | int,
	hie_benefit_query_enabled: bool | int,
	auth_mode: str,
) -> dict:
	"""Save or update an integration profile and run preflight checks."""
	user = require_user()
	system_name = clean(system_name, 140)
	release_version = clean(release_version, 64)
	sandbox_base_url = clean(sandbox_base_url, 256)
	fhir_profile = clean(fhir_profile, 64)
	auth_mode = clean(auth_mode, 64)

	orgs = get_user_orgs(user)
	if not orgs and not is_platform_user(user):
		frappe.throw(_("No vendor organization linked to this account."), frappe.PermissionError)
	vendor = orgs[0] if orgs else "HISI Platform"

	# Validate sandbox URL via network guard (SSRF protection)
	safe_url = normalize_sandbox_url(sandbox_base_url, resolve=True)

	# Execute synchronous preflight probe
	probe = probe_fhir_metadata(safe_url)
	preflight_status = "Ready" if probe.get("ok") else "Failed"
	preflight_note = probe.get("detail") or (
		f"FHIR: {probe.get('fhir')}" if probe.get("fhir") else "Probed successfully"
	)

	# Update existing profile for this system and vendor if found, else create
	existing = frappe.get_all(
		"HISI Integration Profile",
		filters={"vendor": vendor, "system_name": system_name},
		pluck="name",
		limit=1,
	)

	values = {
		"system_name": system_name,
		"release_version": release_version,
		"sandbox_base_url": safe_url,
		"fhir_profile": fhir_profile,
		"shr_visit_id_enabled": 1 if shr_visit_id_enabled else 0,
		"hie_benefit_query_enabled": 1 if hie_benefit_query_enabled else 0,
		"auth_mode": auth_mode,
		"preflight_status": preflight_status,
		"preflight_note": preflight_note,
		"last_verified": frappe.utils.now_datetime(),
	}

	if existing:
		doc = frappe.get_doc("HISI Integration Profile", existing[0])
		doc.update(values)
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{
				"doctype": "HISI Integration Profile",
				"vendor": vendor,
				**values,
			}
		).insert(ignore_permissions=True)

	return ok({"profileId": doc.name, "preflightStatus": preflight_status})
