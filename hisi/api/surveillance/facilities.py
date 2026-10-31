import json
import time

import frappe
from frappe.rate_limiter import rate_limit

from hisi.api.patient import http_get, join_url, simulation_base_url
from hisi.api.response_handler import APIResponse, BadRequest, Forbidden, api_endpoint
from hisi.api.surveillance.audit import log_event
from hisi.utils import active_companies

FIELD_KEYS = ("facility_name", "facility_code", "facility_fid", "operational_status", "latitude", "longitude")
MAX_TERM_LENGTH = 100
RESULT_LIMIT = 20


def duration_ms(start):
	return int((time.time() - start) * 1000)


def facility_search_url():
	"""The DHA Health Facility resource endpoint (HISI Settings).

	``base_url`` + ``fetch_facility`` (e.g. ``https://uat.dha.go.ke`` +
	``/v1/hwr/resource`` + ``Health Facility``); the UAT base is used while
	``enable_uat_simulation`` is set, mirroring the patient flow."""
	doc = frappe.get_doc("HISI Settings")
	if doc.get("enable_uat_simulation"):
		base_url = simulation_base_url()
	else:
		base_url = (doc.get("base_url") or "").strip()
	path = (doc.get("fetch_facility") or "").strip()
	if not base_url or not path:
		raise BadRequest("HISI Settings base_url / fetch_facility are not configured.")
	return join_url(join_url(base_url, path), "Health Facility")


def parse_facility_list(payload):
	rows = payload if isinstance(payload, list) else None
	if rows is None and isinstance(payload, dict):
		rows = payload.get("data")
		if not isinstance(rows, list):
			rows = [payload] if payload.get("facility_name") else []
	if not isinstance(rows, list):
		rows = []
	facilities = []
	for row in rows:
		if not isinstance(row, dict) or not row.get("facility_name"):
			continue
		facilities.append({key: row.get(key) for key in FIELD_KEYS})
	return facilities


@frappe.whitelist(methods=["GET", "POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def search_facilities(term):
	"""Search the DHA facility register for mandate dispatch.

	Returns the facilities matching ``term`` (case-insensitive
	``facility_name LIKE %term%``) with the fields a dispatcher needs to pick
	a surveillance target: facility_name, facility_code, facility_fid,
	operational_status, latitude, longitude."""
	start = time.time()
	user = frappe.session.user
	term = str(term or "").strip()
	if not term:
		log_event(None, user, "error", "facility", None, details={"reason": "term missing"}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest("A search term is required.")
	if len(term) > MAX_TERM_LENGTH:
		log_event(None, user, "error", "facility", None, details={"reason": "term too long"}, outcome="error", duration_ms=duration_ms(start))
		raise BadRequest("The search term is too long ({} characters).".format(MAX_TERM_LENGTH))
	if not active_companies(user):
		log_event(None, user, "scope_denied", "facility", None, outcome="denied", duration_ms=duration_ms(start))
		raise Forbidden("You are not a member of a payer organization.")
	params = {
		"filters": json.dumps([["facility_name", "like", "%{}%".format(term)]]),
		"fields": json.dumps(["*"]),
		"order_by": "facility_name asc",
		"limit_page_length": RESULT_LIMIT,
		"limit_start": 0,
	}
	payload = http_get(facility_search_url(), params)
	facilities = parse_facility_list(payload)
	log_event(None, user, "facilities_searched", "facility", None, details={"term": term, "total": len(facilities)}, outcome="ok", duration_ms=duration_ms(start))
	return APIResponse.success(data={"facilities": facilities, "total": len(facilities)})
