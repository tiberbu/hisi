import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from hisi.api import patient, surveillance
from hisi.api.response_handler import APIResponse
from hisi.api.surveillance import location
from hisi.utils import upsert_org_employee

NAIROBI_LAT = -1.2833
NAIROBI_LNG = 36.8219
MOMBASA_LAT = -4.0435
MOMBASA_LNG = 39.6682
DATA_FMT = "%Y-%m-%d %H:%M:%S"


def point_at_m(lat, lng, meters):
	return lat - meters / 111_132.0, lng


def iso_ts(minutes_ago=0):
	return (datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def checkin(lat=None, lng=None, accuracy_m=18, minutes_ago=0, is_mock=False):
	if lat is None:
		lat = NAIROBI_LAT
	if lng is None:
		lng = NAIROBI_LNG
	return {
		"lat": lat,
		"lng": lng,
		"accuracy_m": accuracy_m,
		"ts": iso_ts(minutes_ago),
		"is_mock": is_mock,
		"platform": "web",
	}


def valid_window():
	now = frappe.utils.now_datetime()
	return (now - timedelta(minutes=1)).strftime(DATA_FMT), (now + timedelta(hours=24)).strftime(DATA_FMT)


def http_response(payload, status_code=200):
	response = MagicMock()
	response.status_code = status_code
	response.json.return_value = payload
	if status_code >= 400:
		error = requests.exceptions.HTTPError("{} error".format(status_code))
		error.response = MagicMock(status_code=status_code)
		response.raise_for_status.side_effect = error
	return response


def patient_payload():
	return {
		"status": 200,
		"message": "Client successfully fetched",
		"count": 1,
		"data": [
			{
				"id": 9000001,
				"first_name": "Amani",
				"middle_name": None,
				"last_name": "Otieno",
				"gender": "MALE",
				"msisdn": "0700000001",
				"date_of_birth": "1990-01-01",
				"cr_id": "CR2473157540001-1",
				"client_status": "Active",
				"identification_type": "National ID",
				"identification_number": "36637305",
			}
		],
	}


def consent_payload():
	return {
		"data": [
			{
				"name": "VCR-20261030-TEST0001",
				"client": "CR2473157540001-1",
				"principal_cr_id": "CR2473157540001-1",
				"status": "Active",
				"consent_status": "Approved",
				"channel": "OTP",
				"verification_status": "Verified",
				"verified_by": "CR2473157540001-1",
				"verification_timestamp": "2026-10-30 08:25:27",
				"facility_id": "FID-47-105963-0",
				"requesting_system": "TEST CLINIC LIMITED",
				"visit_id": "d7fd81f8-bb9f-4f5d-8a8e-fc12014aa0d6",
				"visit_request_id": "TESTREQ001",
			}
		]
	}


def eligibility_payload():
	return {"message": {"status": "Active", "scheme": "SHIF"}}


def fhir_bundle():
	return {
		"resourceType": "Bundle",
		"type": "searchset",
		"total": 2,
		"entry": [
			{"resource": {"resourceType": "Encounter", "id": "enc-1", "status": "finished"}},
			{"resource": {"resourceType": "Condition", "id": "cond-1"}},
		],
	}


class SurveillanceTestCase(IntegrationTestCase):
	def set_settings(self, **values):
		doc = frappe.get_doc("HISI Settings")
		for key, value in values.items():
			doc.set(key, value)
		doc.save(ignore_permissions=True)

	def ensure_role(self, name):
		if not frappe.db.exists("Role", name):
			frappe.get_doc({"doctype": "Role", "role_name": name}).insert(ignore_permissions=True)

	def make_user(self, email, roles):
		for role in roles:
			self.ensure_role(role)
		frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0].replace(".", " ").title(),
			"send_welcome_email": 0,
			"user_type": "System User",
			"roles": [{"role": role} for role in roles],
		}).insert(ignore_permissions=True)
		return email

	def add_member(self, company, user):
		upsert_org_employee(user, company)

	def org_company(self, company):
		return company

	def make_org(self, name):
		# An organization IS its ERPNext Company on this site, so the org id
		# used across the tests is the Company name itself.
		return self.ensure_company(name)

	def ensure_company(self, name):
		row = frappe.get_all("Company", filters={"company_name": name}, fields=["name"], limit=1)
		if row:
			return row[0].name
		return frappe.get_doc({
			"doctype": "Company",
			"company_name": name,
			"country": "Kenya",
			"default_currency": "KES",
		}).insert(ignore_permissions=True).name

	def make_facility(self, name, lat=NAIROBI_LAT, lng=NAIROBI_LNG, radius_km=None):
		doc = frappe.get_doc({
			"doctype": "HISI Health Facility",
			"name": name,
			"facility_code": "KE-{}-0001".format(name[:10].upper().replace(" ", "-")),
			"county": "Nairobi",
			"level": "Hospital",
			"operational_status": "Active",
			"license": "Licence 4471/2024",
			"latitude": lat,
			"longitude": lng,
		})
		if radius_km is not None:
			doc.set("surveillance_radius_km", radius_km)
		doc.insert(ignore_permissions=True)
		return doc.name

	def make_practitioner(self, name, facility):
		doc = frappe.get_doc({
			"doctype": "HISI Professional Record",
			"name": name,
			"cadre": "Medical Officer",
			"regulator": "KMPDC",
			"registration_number": "KMPDC/4471",
			"facility": facility,
		}).insert(ignore_permissions=True)
		return doc.name

	def set_dha_settings(self):
		self.set_settings(
			base_url="https://api.dha.go.ke",
			fetch_client="/client-registry/fetch-client",
			fetch_facility="/v1/hwr/resource",
			eligibility_check="/backend/api/v5/coverage/validate",
			fetch_patient_consent="/v1/cmp/resource/Visit Consent",
			consumer_key="KEY",
			consumer_secret="SECRET",
			uat_base_url="https://uat.dha.go.ke",
			enable_uat_simulation=1,
			jwt_ttl_s=60,
			upstream_api_timeout=60,
		)

	def set_aws_settings(self):
		self.set_settings(
			data_store_id="0123456789abcdef0123456789abcdef01",
			aws_access_key="AKIAEXAMPLEKEY00000001",
			aws_secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
			aws_region="eu-west-2",
			query_mapper=[{"param_key": "_include", "param_value": "Encounter:service-provider"}],
		)

	def setUp(self):
		super().setUp()
		# Neutralise commits for the duration of the test: frappe-core paths
		# (EmailQueue.send, auth.update_profile) call frappe.db.commit(), which
		# releases this class's savepoint and leaks fixtures into the next test
		# (a single poisoned row then cascades into DuplicateEntryError across
		# the whole suite). No-op'ing commit keeps the savepoint intact so the
		# tearDown rollback restores a clean slate for every subclass.
		# frappe.db is a thread-local proxy, so patch commit on the concrete DB
		# class (located via the real instance's MRO), not on the proxy type.
		_db_cls = type(frappe.local.db)
		_commit_owner = next(c for c in _db_cls.__mro__ if "commit" in vars(c))
		self._commit_patcher = patch.object(_commit_owner, "commit", lambda self: None)
		self._commit_patcher.start()
		frappe.db.savepoint("surveillance_test")
		self.set_settings(
			surveillance_session_ttl_minutes=240,
			surveillance_idle_timeout_minutes=30,
			surveillance_default_radius_km=1.0,
			surveillance_max_accuracy_m=200,
			surveillance_strict_location=1,
			surveillance_recheck_enabled=1,
			surveillance_verify_lockout_attempts=5,
			surveillance_retention_days=2192,
		)
		self.set_dha_settings()
		self.org = self.make_org("Acme Assurance")
		self.org_b = self.make_org("Beta Assurance")
		# No custom roles exist any more: access comes from the standard role
		# matrices plus Company user permissions; these users are scoped through
		# their Employee membership (add_member) or are plain members (officers).
		self.dispatcher = self.make_user("dispatcher@acme.example", [])
		self.add_member(self.org, self.dispatcher)
		self.dispatcher_b = self.make_user("dispatcher@beta.example", [])
		self.add_member(self.org_b, self.dispatcher_b)
		self.officer = self.make_user("officer@acme.example", [])
		self.other_officer = self.make_user("other@acme.example", [])
		self.facility = self.make_facility("Nairobi East Clinic")
		self.facility_b = self.make_facility("Mombasa Coast Hospital", MOMBASA_LAT, MOMBASA_LNG)

	def tearDown(self):
		frappe.db.rollback(save_point="surveillance_test")
		# Test bodies call frappe.set_user(); the rollback removes those users,
		# so restore a neutral session or the next setUp's user insert fails
		# link validation (Notification Settings defaults user to __user).
		frappe.set_user("Administrator")
		self._commit_patcher.stop()
		super().tearDown()

	def issue_mandate(self, **overrides):
		now = frappe.utils.now_datetime()
		payload = {
			"facility": self.facility,
			"assigned_officer": self.officer,
			"reason": "Suspicion on claim CLM-2026-0441",
			"scope_lines": [{"line_type": "patient", "patient_identifier": "36637305", "note": "12 Oct visit"}],
			"valid_from": (now - timedelta(minutes=1)).strftime(DATA_FMT),
			"expires_at": (now + timedelta(hours=24)).strftime(DATA_FMT),
			"radius_km": None,
			"location_policy": "strict",
		}
		payload.update(overrides)
		frappe.set_user(self.dispatcher)
		surveillance.issue_mandate(**payload)

	def issue_code(self, **overrides):
		self.issue_mandate(**overrides)
		return frappe.local.response["data"]["short_code"]

	def start_session(self, code, **overrides):
		payload = {
			"mandate_code": code,
			"location": checkin(),
			"device": {"app_version": "1.4.2", "user_agent": "test-agent"},
		}
		payload.update(overrides)
		frappe.set_user(self.officer)
		surveillance.start_session(**payload)

	def session_name(self, session_no):
		return frappe.get_all("HISI Surveillance Session", filters={"session_no": session_no}, pluck="name")[0]

	def event_count(self):
		return frappe.db.count("HISI Surveillance Event")

	def new_events(self, before):
		rows = frappe.get_all(
			"HISI Surveillance Event",
			fields=["event_type", "outcome", "session", "target_type", "target_ref_masked"],
			order_by="creation asc, name asc",
		)
		return rows[before:]


class TestGeofenceUnit(SurveillanceTestCase):
	def test_haversine_zero_for_same_point(self):
		self.assertEqual(location.haversine_m(NAIROBI_LAT, NAIROBI_LNG, NAIROBI_LAT, NAIROBI_LNG), 0)

	def test_haversine_nairobi_to_mombasa_band(self):
		distance = location.haversine_m(NAIROBI_LAT, NAIROBI_LNG, MOMBASA_LAT, MOMBASA_LNG)
		self.assertGreater(distance, 430_000)
		self.assertLess(distance, 450_000)

	def test_radius_boundary_one_metre_inside_is_verified(self):
		lat, lng = point_at_m(NAIROBI_LAT, NAIROBI_LNG, 999)
		result = location.evaluate_checkin(checkin(lat, lng), NAIROBI_LAT, NAIROBI_LNG, 1.0, 200)
		self.assertEqual(result.status, "verified")
		self.assertLess(result.distance_m, 1000)

	def test_radius_boundary_one_metre_outside_is_out_of_range(self):
		lat, lng = point_at_m(NAIROBI_LAT, NAIROBI_LNG, 1001)
		result = location.evaluate_checkin(checkin(lat, lng), NAIROBI_LAT, NAIROBI_LNG, 1.0, 200)
		self.assertEqual(result.status, "out_of_range")
		self.assertGreater(result.distance_m, 1000)

	def test_matrix_fresh_fix_inside_is_verified(self):
		result = location.evaluate_checkin(checkin(minutes_ago=1), NAIROBI_LAT, NAIROBI_LNG, 1.0, 200)
		self.assertEqual(result.status, "verified")

	def test_matrix_stale_timestamp_is_out_of_range(self):
		result = location.evaluate_checkin(checkin(minutes_ago=5), NAIROBI_LAT, NAIROBI_LNG, 1.0, 200)
		self.assertEqual(result.status, "out_of_range")

	def test_matrix_low_accuracy_is_out_of_range(self):
		result = location.evaluate_checkin(checkin(accuracy_m=500), NAIROBI_LAT, NAIROBI_LNG, 1.0, 200)
		self.assertEqual(result.status, "out_of_range")

	def test_matrix_mock_location_is_spoofed(self):
		result = location.evaluate_checkin(checkin(is_mock=True), NAIROBI_LAT, NAIROBI_LNG, 1.0, 200)
		self.assertEqual(result.status, "spoofed")

	def test_matrix_missing_payload_is_unreported(self):
		result = location.evaluate_checkin(None, NAIROBI_LAT, NAIROBI_LNG, 1.0, 200)
		self.assertEqual(result.status, "unreported")
		self.assertIsNone(result.distance_m)

	def test_distance_recorded_even_out_of_range(self):
		lat, lng = point_at_m(NAIROBI_LAT, NAIROBI_LNG, 1500)
		result = location.evaluate_checkin(checkin(lat, lng), NAIROBI_LAT, NAIROBI_LNG, 1.0, 200)
		self.assertEqual(result.status, "out_of_range")
		self.assertAlmostEqual(result.distance_m, 1500, delta=15)

	def test_radius_resolution_mandate_wins(self):
		self.assertEqual(location.resolve_radius_km(0.5, 0.7, 1.0), 0.5)

	def test_radius_resolution_facility_beats_settings(self):
		self.assertEqual(location.resolve_radius_km(None, 0.7, 1.0), 0.7)

	def test_radius_resolution_settings_last_resort(self):
		self.assertEqual(location.resolve_radius_km(None, None, 1.0), 1.0)

	def test_radius_resolution_unset_settings_fail_fast(self):
		self.assertRaises(APIResponse, location.resolve_radius_km, None, None, None)

	def test_radius_resolution_stored_zero_is_unset_sentinel(self):
		# Float columns are NOT NULL DEFAULT 0.0; an unset stored 0.0 must fall through
		self.assertEqual(location.resolve_radius_km(0.0, 0.2, 1.0), 0.2)
		self.assertEqual(location.resolve_radius_km(0.0, 0.0, 1.0), 1.0)

	def test_radius_resolution_negative_still_rejected(self):
		self.assertRaises(APIResponse, location.resolve_radius_km, -1.0, 0.7, 1.0)


class TestAuditCompleteness(SurveillanceTestCase):
	def test_issue_success_writes_mandate_issued(self):
		before = self.event_count()
		self.issue_mandate()
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "mandate_issued")
		self.assertEqual(events[0]["outcome"], "ok")
		self.assertIsNone(events[0]["session"])

	def test_issue_bad_payload_writes_error_event(self):
		before = self.event_count()
		frappe.set_user(self.dispatcher)
		surveillance.issue_mandate(
			facility=self.facility,
			assigned_officer=self.officer,
			reason="x",
			scope_lines=[],
			valid_from=valid_window()[0],
			expires_at=valid_window()[1],
		)
		self.assertEqual(frappe.local.response.get("code"), "BAD_REQUEST")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "error")
		self.assertEqual(events[0]["outcome"], "error")

	def test_issue_by_non_provider_writes_scope_denied(self):
		before = self.event_count()
		frappe.set_user(self.officer)
		surveillance.issue_mandate(
			facility=self.facility,
			assigned_officer=self.officer,
			reason="x",
			scope_lines=[{"line_type": "patient", "patient_identifier": "36637305"}],
			valid_from=valid_window()[0],
			expires_at=valid_window()[1],
		)
		self.assertEqual(frappe.local.response.get("code"), "FORBIDDEN")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "scope_denied")
		self.assertEqual(events[0]["outcome"], "denied")

	def test_verify_success_writes_mandate_verified(self):
		code = self.issue_code()
		before = self.event_count()
		frappe.set_user(self.officer)
		surveillance.verify_mandate(code=code)
		self.assertEqual(frappe.local.response.get("status"), "success")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "mandate_verified")
		self.assertEqual(events[0]["outcome"], "ok")

	def test_verify_by_wrong_officer_writes_verify_failed(self):
		code = self.issue_code()
		before = self.event_count()
		frappe.set_user(self.other_officer)
		surveillance.verify_mandate(code=code)
		self.assertEqual(frappe.local.response.get("code"), "MANDATE_INVALID")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "mandate_verify_failed")
		self.assertEqual(events[0]["outcome"], "denied")

	def test_verify_unknown_code_writes_failed_session_null(self):
		before = self.event_count()
		frappe.set_user(self.officer)
		surveillance.verify_mandate(code="ZZZZZZZZ")
		self.assertEqual(frappe.local.response.get("code"), "MANDATE_INVALID")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "mandate_verify_failed")
		self.assertIsNone(events[0]["session"])

	def test_fifth_failure_locks_code_with_lock_event(self):
		code = self.issue_code()
		frappe.set_user(self.other_officer)
		for _ in range(4):
			surveillance.verify_mandate(code=code)
			self.assertEqual(frappe.local.response.get("code"), "MANDATE_INVALID")
		before = self.event_count()
		surveillance.verify_mandate(code=code)
		self.assertEqual(frappe.local.response.get("code"), "MANDATE_INVALID")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "mandate_verify_locked")
		self.assertEqual(events[0]["outcome"], "denied")
		frappe.set_user(self.officer)
		surveillance.verify_mandate(code=code)
		self.assertEqual(frappe.local.response.get("code"), "MANDATE_INVALID")

	def test_start_success_writes_checkin_then_started(self):
		code = self.issue_code()
		before = self.event_count()
		self.start_session(code)
		self.assertEqual(frappe.local.response.get("status"), "success")
		events = self.new_events(before)
		self.assertEqual([e["event_type"] for e in events], ["checkin_verified", "session_started"])
		self.assertEqual(events[0]["outcome"], "ok")
		self.assertEqual(events[1]["outcome"], "ok")

	def test_start_strict_failure_writes_checkin_failed(self):
		code = self.issue_code()
		before = self.event_count()
		frappe.set_user(self.officer)
		surveillance.start_session(mandate_code=code, location=checkin(MOMBASA_LAT, MOMBASA_LNG), device={"app_version": "1.4.2", "user_agent": "test-agent"})
		self.assertEqual(frappe.local.response.get("code"), "LOCATION_CHECK_FAILED")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "checkin_failed")
		self.assertIsNone(events[0]["session"])

	def test_start_by_wrong_officer_writes_auth_denied(self):
		code = self.issue_code()
		before = self.event_count()
		frappe.set_user(self.other_officer)
		surveillance.start_session(mandate_code=code, location=checkin(), device={"app_version": "1.4.2", "user_agent": "test-agent"})
		self.assertEqual(frappe.local.response.get("code"), "UNAUTHORIZED")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "auth_denied")
		self.assertIsNone(events[0]["session"])

	def test_missing_session_id_writes_session_null_event(self):
		before = self.event_count()
		frappe.set_user(self.officer)
		surveillance.get_facility(session_id="")
		self.assertEqual(frappe.local.response.get("status"), "error")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertIsNone(events[0]["session"])
		self.assertIn(events[0]["event_type"], ("auth_denied", "error"))

	def test_officer_get_session_writes_no_event(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		before = self.event_count()
		frappe.set_user(self.officer)
		surveillance.get_session(session_id=session_no)
		self.assertEqual(frappe.local.response.get("status"), "success")
		self.assertEqual(self.new_events(before), [])

	def test_surveil_patient_writes_one_event_per_step(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		before = self.event_count()
		with patch.object(patient.requests, "get", return_value=http_response(patient_payload())):
			frappe.set_user(self.officer)
			surveillance.surveil_patient(session_id=session_no, steps=["patient"], filters={"identification_type": "National ID", "identification_number": "36637305"})
		self.assertEqual(frappe.local.response.get("status"), "success")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "patient_viewed")
		self.assertEqual(events[0]["outcome"], "ok")

	def test_surveil_out_of_scope_writes_scope_denied(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		before = self.event_count()
		with patch.object(patient.requests, "get") as mock_get:
			frappe.set_user(self.officer)
			surveillance.surveil_patient(session_id=session_no, steps=["patient"], filters={"identification_type": "CR ID", "identification_number": "CR2473157549999-9"})
		self.assertEqual(frappe.local.response.get("code"), "FORBIDDEN")
		self.assertEqual(mock_get.call_count, 0)
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "scope_denied")
		self.assertEqual(events[0]["outcome"], "denied")

	def test_upstream_failure_writes_error_event(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		before = self.event_count()
		with patch.object(patient.requests, "get", return_value=http_response(None, status_code=502)):
			frappe.set_user(self.officer)
			surveillance.surveil_patient(session_id=session_no, steps=["patient"], filters={"identification_type": "National ID", "identification_number": "36637305"})
		self.assertEqual(frappe.local.response.get("code"), "UPSTREAM_ERROR")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "error")
		self.assertEqual(events[0]["outcome"], "error")

	def test_facility_and_practitioner_reads_write_their_events(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		frappe.set_user(self.officer)
		before = self.event_count()
		surveillance.get_facility(session_id=session_no)
		self.assertEqual(frappe.local.response.get("status"), "success")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "facility_viewed")
		before = self.event_count()
		surveillance.list_practitioners(session_id=session_no)
		self.assertEqual(frappe.local.response.get("status"), "success")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "practitioner_viewed")

	def test_recheck_writes_session_recheck(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		before = self.event_count()
		frappe.set_user(self.officer)
		surveillance.recheck_location(session_id=session_no, location=checkin(), device={"app_version": "1.4.2", "user_agent": "test-agent"})
		self.assertEqual(frappe.local.response.get("status"), "success")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "session_recheck")

	def test_end_session_writes_session_ended(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		before = self.event_count()
		frappe.set_user(self.officer)
		surveillance.end_session(session_id=session_no, summary="done")
		self.assertEqual(frappe.local.response.get("status"), "success")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "session_ended")

	def test_list_sessions_writes_sessions_listed(self):
		code = self.issue_code()
		self.start_session(code)
		frappe.set_user(self.dispatcher)
		before = self.event_count()
		surveillance.list_sessions()
		self.assertEqual(frappe.local.response.get("status"), "success")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "sessions_listed")
		self.assertIsNone(events[0]["session"])

	def test_list_mandates_writes_mandate_listed(self):
		self.issue_code()
		frappe.set_user(self.dispatcher)
		before = self.event_count()
		surveillance.list_mandates()
		self.assertEqual(frappe.local.response.get("status"), "success")
		data = frappe.local.response["data"]
		self.assertEqual(data["total"], 1)
		self.assertEqual(data["mandates"][0]["status"], "dispatched")
		self.assertGreaterEqual(data["mandates"][0]["scope_counts"]["patient"], 1)
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "mandate_listed")
		self.assertIsNone(events[0]["session"])

	def test_get_session_audit_writes_audit_viewed(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		frappe.set_user(self.dispatcher)
		before = self.event_count()
		surveillance.get_session_audit(session_id=session_no)
		self.assertEqual(frappe.local.response.get("status"), "success")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "audit_viewed")

	def test_revoke_writes_mandate_revoked_plus_session_revoked(self):
		self.issue_mandate()
		data = frappe.local.response["data"]
		code, mandate_no = data["short_code"], data["mandate_no"]
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		frappe.set_user(self.dispatcher)
		before = self.event_count()
		surveillance.revoke_mandate(mandate_no=mandate_no)
		self.assertEqual(frappe.local.response.get("status"), "success")
		events = self.new_events(before)
		self.assertEqual([e["event_type"] for e in events], ["mandate_revoked", "session_revoked"])

	def test_failed_event_insert_degrades_audit_not_call(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		real_get_doc = frappe.get_doc

		def deny_event_insert(*args, **kwargs):
			first = args[0] if args else kwargs.get("doctype")
			if isinstance(first, dict):
				is_event = first.get("doctype") == "HISI Surveillance Event"
			else:
				is_event = first == "HISI Surveillance Event"
			if is_event:
				raise frappe.ValidationError("simulated insert failure")
			return real_get_doc(*args, **kwargs)

		with patch.object(frappe, "get_doc", side_effect=deny_event_insert):
			frappe.set_user(self.officer)
			surveillance.get_facility(session_id=session_no)
		self.assertEqual(frappe.local.response.get("status"), "success")
		flags = frappe.get_all("HISI Surveillance Session", filters={"session_no": session_no}, pluck="checkin_flags")[0]
		self.assertIn("audit_degraded", json.loads(flags or "[]"))
		self.assertTrue(frappe.get_all("Error Log", fields=["name"], limit=1))

	def test_event_doctype_grants_no_delete_anywhere(self):
		doc = frappe.get_doc("DocType", "HISI Surveillance Event")
		for perm in doc.permissions:
			self.assertFalse(int(perm.delete or 0))

	def test_officer_cannot_delete_event_directly(self):
		code = self.issue_code()
		self.start_session(code)
		frappe.set_user(self.officer)
		rows = frappe.get_all("HISI Surveillance Event", fields=["name"], limit=1)
		self.assertTrue(rows)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("HISI Surveillance Event", rows[0]["name"])


class TestMandateLifecycle(SurveillanceTestCase):
	def test_issue_returns_code_qr_and_signature(self):
		self.issue_mandate()
		data = frappe.local.response["data"]
		self.assertEqual(data["status"], "dispatched")
		self.assertEqual(len(data["short_code"]), 8)
		self.assertRegex(data["short_code"], "^[0-9A-F]{8}$")
		self.assertIn("hisi.api.surveillance/verify_mandate?code=" + data["short_code"], data["qr_url"])
		self.assertTrue(data["signature"])
		rows = frappe.get_all("HISI Surveillance Mandate", filters={"short_code": data["short_code"]}, fields=["company", "status"])
		self.assertEqual(rows[0]["company"], self.org)
		self.assertEqual(rows[0]["status"], "dispatched")

	def test_second_issue_gives_distinct_code_and_signature(self):
		self.issue_mandate()
		first = frappe.local.response["data"]
		self.issue_mandate()
		second = frappe.local.response["data"]
		self.assertNotEqual(first["short_code"], second["short_code"])
		self.assertNotEqual(first["signature"], second["signature"])

	def test_issue_validation_errors(self):
		valid_from, expires_at = valid_window()
		frappe.set_user(self.dispatcher)
		surveillance.issue_mandate(facility=self.facility, assigned_officer=self.officer, reason="x", scope_lines=[], valid_from=valid_from, expires_at=expires_at)
		self.assertEqual(frappe.local.response.get("code"), "BAD_REQUEST")
		surveillance.issue_mandate(facility=self.facility, assigned_officer=self.officer, reason="x", scope_lines=[{"line_type": "patient", "patient_identifier": "36637305"}], valid_from=expires_at, expires_at=valid_from)
		self.assertEqual(frappe.local.response.get("code"), "BAD_REQUEST")
		surveillance.issue_mandate(facility=self.facility, assigned_officer="ghost@example.com", reason="x", scope_lines=[{"line_type": "patient", "patient_identifier": "36637305"}], valid_from=valid_from, expires_at=expires_at)
		self.assertEqual(frappe.local.response.get("code"), "BAD_REQUEST")

	def test_new_facility_created_from_registry_on_issue(self):
		registry_row = {
			"facility_name": "Kikuyu Gate Clinic",
			"facility_code": "KE-KGC-0001",
			"facility_fid": "FID-KGC-1",
			"operational_status": "Active",
			"latitude": NAIROBI_LAT,
			"longitude": NAIROBI_LNG,
		}
		with patch("hisi.api.surveillance.mandate.http_get", return_value=[registry_row]) as mocked:
			frappe.set_user(self.dispatcher)
			surveillance.issue_mandate(
				facility="Kikuyu Gate Clinic",
				assigned_officer=self.officer,
				reason="x",
				scope_lines=[{"line_type": "patient", "patient_identifier": "36637305"}],
				valid_from=valid_window()[0],
				expires_at=valid_window()[1],
			)
		self.assertEqual(frappe.local.response.get("status"), "success")
		rows = frappe.get_all("HISI Health Facility", filters={"facility_code": "KE-KGC-0001"}, fields=["name", "latitude", "longitude", "registered_by"])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["name"], "Kikuyu Gate Clinic")
		self.assertEqual(rows[0]["latitude"], NAIROBI_LAT)
		self.assertEqual(rows[0]["longitude"], NAIROBI_LNG)
		self.assertEqual(rows[0]["registered_by"], self.org)
		self.assertTrue(mocked.called)

	def test_new_facility_not_in_registry_rejected(self):
		with patch("hisi.api.surveillance.mandate.http_get", return_value=[]) as mocked:
			frappe.set_user(self.dispatcher)
			surveillance.issue_mandate(
				facility="No Coordinates Clinic",
				assigned_officer=self.officer,
				reason="x",
				scope_lines=[{"line_type": "patient", "patient_identifier": "36637305"}],
				valid_from=valid_window()[0],
				expires_at=valid_window()[1],
			)
		self.assertEqual(frappe.local.response.get("code"), "BAD_REQUEST")
		self.assertEqual(frappe.get_all("HISI Health Facility", filters={"name": "No Coordinates Clinic"}), [])
		self.assertTrue(mocked.called)

	def test_expired_mandate_is_invalid_and_flips_lazy(self):
		now = frappe.utils.now_datetime()
		self.issue_mandate(valid_from=(now - timedelta(hours=2)).strftime(DATA_FMT), expires_at=(now - timedelta(hours=1)).strftime(DATA_FMT))
		code = frappe.local.response["data"]["short_code"]
		frappe.set_user(self.officer)
		surveillance.verify_mandate(code=code)
		self.assertEqual(frappe.local.response.get("code"), "MANDATE_INVALID")
		self.assertEqual(frappe.get_all("HISI Surveillance Mandate", filters={"short_code": code}, pluck="status")[0], "expired")

	def test_verify_response_has_no_pii(self):
		code = self.issue_code()
		frappe.set_user(self.officer)
		surveillance.verify_mandate(code=code)
		data = frappe.local.response["data"]
		self.assertTrue(data["valid"])
		self.assertTrue(data["assigned_to_me"])
		self.assertEqual(data["scope_counts"], {"patient": 1})
		self.assertEqual(data["facility"]["name"], "Nairobi East Clinic")
		self.assertNotIn("36637305", json.dumps(data))

	def test_invalid_message_identical_for_all_failures(self):
		code = self.issue_code()
		frappe.set_user(self.other_officer)
		surveillance.verify_mandate(code=code)
		message_not_mine = frappe.local.response.get("message")
		frappe.set_user(self.officer)
		surveillance.verify_mandate(code="ZZZZZZZZ")
		self.assertEqual(frappe.local.response.get("message"), message_not_mine)

	def test_revoke_kills_active_sessions_and_blocks_calls(self):
		self.issue_mandate()
		data = frappe.local.response["data"]
		code, mandate_no = data["short_code"], data["mandate_no"]
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		frappe.set_user(self.dispatcher)
		surveillance.revoke_mandate(mandate_no=mandate_no)
		rdata = frappe.local.response["data"]
		self.assertEqual(rdata["status"], "revoked")
		self.assertEqual(rdata["revoked_sessions"], 1)
		self.assertEqual(frappe.get_all("HISI Surveillance Session", filters={"session_no": session_no}, pluck="status")[0], "revoked")
		frappe.set_user(self.officer)
		surveillance.get_facility(session_id=session_no)
		self.assertEqual(frappe.local.response.get("code"), "MANDATE_REVOKED")
		self.assertEqual(frappe.local.response.get("http_status_code"), 403)

	def test_revoke_twice_conflicts(self):
		self.issue_mandate()
		mandate_no = frappe.local.response["data"]["mandate_no"]
		frappe.set_user(self.dispatcher)
		surveillance.revoke_mandate(mandate_no=mandate_no)
		self.assertEqual(frappe.local.response.get("status"), "success")
		surveillance.revoke_mandate(mandate_no=mandate_no)
		self.assertEqual(frappe.local.response.get("code"), "CONFLICT")

	def test_cross_payer_revoke_forbidden(self):
		self.issue_mandate()
		mandate_no = frappe.local.response["data"]["mandate_no"]
		frappe.set_user(self.dispatcher_b)
		surveillance.revoke_mandate(mandate_no=mandate_no)
		self.assertEqual(frappe.local.response.get("code"), "FORBIDDEN")


class TestSessionLifecycle(SurveillanceTestCase):
	def test_full_cycle_start_get_end(self):
		self.issue_mandate()
		code = frappe.local.response["data"]["short_code"]
		self.start_session(code)
		data = frappe.local.response["data"]
		session_no = data["session_no"]
		self.assertEqual(data["status"], "active")
		self.assertEqual(data["location_status"], "verified")
		self.assertLess(data["checkin_distance_m"], 1000)
		self.assertEqual(frappe.get_all("HISI Surveillance Mandate", filters={"short_code": code}, pluck="status")[0], "active")
		frappe.set_user(self.officer)
		surveillance.get_session(session_id=session_no)
		gs = frappe.local.response["data"]
		self.assertEqual(gs["session"]["status"], "active")
		self.assertGreater(gs["time_remaining_sec"], 0)
		self.assertNotIn("36637305", json.dumps(gs))
		self.assertTrue(gs["scope_lines"][0]["patient_ref_masked"])
		surveillance.end_session(session_id=session_no, summary="Records match claim.")
		ed = frappe.local.response["data"]
		self.assertEqual(ed["status"], "completed")
		self.assertTrue(ed["ended_at"])

	def test_strict_out_of_range_creates_no_session(self):
		code = self.issue_code()
		frappe.set_user(self.officer)
		surveillance.start_session(mandate_code=code, location=checkin(MOMBASA_LAT, MOMBASA_LNG), device={"app_version": "1.4.2", "user_agent": "test-agent"})
		response = frappe.local.response
		self.assertEqual(response.get("code"), "LOCATION_CHECK_FAILED")
		self.assertRegex(response.get("message"), r"\d")
		self.assertEqual(frappe.db.count("HISI Surveillance Session"), 0)
		self.assertEqual(frappe.get_all("HISI Surveillance Mandate", filters={"short_code": code}, pluck="status")[0], "dispatched")

	def test_flag_mode_creates_flagged_session(self):
		self.set_settings(surveillance_strict_location=0)
		code = self.issue_code()
		frappe.set_user(self.officer)
		surveillance.start_session(mandate_code=code, location=checkin(MOMBASA_LAT, MOMBASA_LNG), device={"app_version": "1.4.2", "user_agent": "test-agent"})
		self.assertEqual(frappe.local.response.get("status"), "success")
		session_no = frappe.local.response["data"]["session_no"]
		rows = frappe.get_all("HISI Surveillance Session", filters={"session_no": session_no}, fields=["location_status", "checkin_flags"])
		self.assertEqual(rows[0]["location_status"], "out_of_range")
		self.assertIn("out_of_range", json.loads(rows[0]["checkin_flags"] or "[]"))

	def test_facility_radius_beats_settings_default(self):
		facility = self.make_facility("Tight Radius Clinic", radius_km=0.2)
		self.issue_mandate(facility=facility)
		code = frappe.local.response["data"]["short_code"]
		lat, lng = point_at_m(NAIROBI_LAT, NAIROBI_LNG, 300)
		frappe.set_user(self.officer)
		surveillance.start_session(mandate_code=code, location=checkin(lat, lng), device={"app_version": "1.4.2", "user_agent": "test-agent"})
		self.assertEqual(frappe.local.response.get("code"), "LOCATION_CHECK_FAILED")

	def test_mandate_radius_override_wins(self):
		facility = self.make_facility("Wide Radius Clinic", radius_km=0.2)
		self.issue_mandate(facility=facility, radius_km=5.0)
		code = frappe.local.response["data"]["short_code"]
		lat, lng = point_at_m(NAIROBI_LAT, NAIROBI_LNG, 300)
		self.start_session(code, location=checkin(lat, lng))
		self.assertEqual(frappe.local.response.get("status"), "success")

	def test_idle_timeout_expires_lazily(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		name = self.session_name(session_no)
		frappe.db.set_value("HISI Surveillance Session", name, "last_activity", (frappe.utils.now_datetime() - timedelta(minutes=31)).strftime(DATA_FMT))
		frappe.set_user(self.officer)
		before = self.event_count()
		surveillance.get_session(session_id=session_no)
		self.assertEqual(frappe.local.response.get("code"), "FORBIDDEN")
		self.assertEqual(frappe.get_all("HISI Surveillance Session", filters={"session_no": session_no}, pluck="status")[0], "expired")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "session_expired")

	def test_ttl_expires_lazily(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		name = self.session_name(session_no)
		frappe.db.set_value("HISI Surveillance Session", name, "ends_at", (frappe.utils.now_datetime() - timedelta(hours=1)).strftime(DATA_FMT))
		frappe.set_user(self.officer)
		surveillance.get_session(session_id=session_no)
		self.assertEqual(frappe.local.response.get("code"), "FORBIDDEN")
		self.assertEqual(frappe.get_all("HISI Surveillance Session", filters={"session_no": session_no}, pluck="status")[0], "expired")

	def test_recheck_verified_then_impossible_jump_flagged(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		frappe.set_user(self.officer)
		surveillance.recheck_location(session_id=session_no, location=checkin(), device={"app_version": "1.4.2", "user_agent": "test-agent"})
		self.assertEqual(frappe.local.response["data"]["location_status"], "verified")
		surveillance.recheck_location(session_id=session_no, location=checkin(MOMBASA_LAT, MOMBASA_LNG), device={"app_version": "1.4.2", "user_agent": "test-agent"})
		data = frappe.local.response["data"]
		self.assertIn("impossible_move", data["flagged"])
		rows = frappe.get_all("HISI Surveillance Session", filters={"session_no": session_no}, fields=["checkin_flags"])
		self.assertIn("impossible_move", json.loads(rows[0]["checkin_flags"] or "[]"))

	def test_recheck_disabled_is_bad_request(self):
		self.set_settings(surveillance_recheck_enabled=0)
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		frappe.set_user(self.officer)
		surveillance.recheck_location(session_id=session_no, location=checkin(), device={"app_version": "1.4.2", "user_agent": "test-agent"})
		self.assertEqual(frappe.local.response.get("code"), "BAD_REQUEST")

	def test_end_twice_conflicts(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		frappe.set_user(self.officer)
		surveillance.end_session(session_id=session_no, summary="done")
		self.assertEqual(frappe.local.response.get("status"), "success")
		surveillance.end_session(session_id=session_no, summary="again")
		self.assertEqual(frappe.local.response.get("code"), "CONFLICT")

	def test_get_session_by_other_officer_rejected(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		frappe.set_user(self.other_officer)
		surveillance.get_session(session_id=session_no)
		self.assertIn(frappe.local.response.get("code"), ("FORBIDDEN", "NOT_FOUND"))

	def test_list_practitioners_only_surveilled_facility(self):
		practitioner = self.make_practitioner("Dr. A. Wanjiku", self.facility)
		other = self.make_practitioner("Dr. B. Otieno", self.facility_b)
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		frappe.set_user(self.officer)
		surveillance.list_practitioners(session_id=session_no)
		data = frappe.local.response["data"]
		self.assertEqual([p["record_id"] for p in data["practitioners"]], [practitioner])

	def test_get_facility_hides_geofence_internals(self):
		facility = self.make_facility("Radiused Clinic", radius_km=2.5)
		self.issue_mandate(facility=facility)
		code = frappe.local.response["data"]["short_code"]
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		frappe.set_user(self.officer)
		surveillance.get_facility(session_id=session_no)
		data = frappe.local.response["data"]
		self.assertEqual(data["name"], "Radiused Clinic")
		self.assertNotIn("surveillance_radius_km", json.dumps(data))

	def test_surveiled_refs_recorded_per_use(self):
		self.make_practitioner("Dr. A. Wanjiku", self.facility)
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		with patch.object(patient.requests, "get", return_value=http_response(patient_payload())):
			frappe.set_user(self.officer)
			surveillance.surveil_patient(session_id=session_no, steps=["patient"], filters={"identification_type": "National ID", "identification_number": "36637305"})
		frappe.set_user(self.officer)
		surveillance.list_practitioners(session_id=session_no)
		refs = frappe.get_all("HISI Surveillance Session", filters={"session_no": session_no}, pluck="surveiled_refs")[0]
		ref_types = [r["type"] for r in json.loads(refs or "[]")]
		self.assertIn("patient", ref_types)
		self.assertIn("practitioner", ref_types)


class TestIntegrationMockedUpstream(SurveillanceTestCase):
	def setUp(self):
		SurveillanceTestCase.setUp(self)
		self.set_aws_settings()

	def test_surveil_patient_shape_identical_to_patient_flow(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		with patch.object(patient.requests, "get", return_value=http_response(patient_payload())):
			frappe.set_user(self.officer)
			surveillance.surveil_patient(session_id=session_no, steps=["patient"], filters={"identification_type": "National ID", "identification_number": "36637305"})
		data = frappe.local.response["data"]
		self.assertEqual(data["cr_id"], "9000001")
		self.assertEqual(data["steps"], ["patient"])
		self.assertEqual(data["results"]["patient"]["cr_id"], "9000001")
		self.assertEqual(data["results"]["patient"]["name"], "Amani Otieno")
		self.assertTrue(data["simulation"])

	def test_full_journey_all_four_steps(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		with patch.object(patient.requests, "get", side_effect=[http_response(patient_payload()), http_response(consent_payload()), http_response(fhir_bundle())]), patch.object(
			patient.requests, "post", return_value=http_response(eligibility_payload())
		):
			frappe.set_user(self.officer)
			surveillance.surveil_patient(session_id=session_no, steps=["patient", "eligibility", "consent", "records"], filters={"identification_type": "CR ID", "identification_number": "36637305"})
		data = frappe.local.response["data"]
		self.assertEqual(set(data["results"]), {"patient", "eligibility", "consent", "records"})
		self.assertEqual(data["results"]["records"]["visit_id"], "d7fd81f8-bb9f-4f5d-8a8e-fc12014aa0d6")
		self.assertEqual([r.get("resourceType") for r in data["results"]["records"]["records"]], ["Encounter", "Condition"])

	def test_full_journey_writes_four_step_events(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		before = self.event_count()
		with patch.object(patient.requests, "get", side_effect=[http_response(patient_payload()), http_response(consent_payload()), http_response(fhir_bundle())]), patch.object(
			patient.requests, "post", return_value=http_response(eligibility_payload())
		):
			frappe.set_user(self.officer)
			surveillance.surveil_patient(session_id=session_no, steps=["patient", "eligibility", "consent", "records"], filters={"identification_type": "CR ID", "identification_number": "36637305"})
		events = self.new_events(before)
		self.assertEqual([e["event_type"] for e in events], ["patient_viewed", "eligibility_viewed", "consent_viewed", "records_viewed"])
		self.assertTrue(all(e["outcome"] == "ok" for e in events))

	def test_upstream_failure_propagates_session_survives(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		with patch.object(patient.requests, "get", return_value=http_response(None, status_code=502)):
			frappe.set_user(self.officer)
			surveillance.surveil_patient(session_id=session_no, steps=["patient"], filters={"identification_type": "National ID", "identification_number": "36637305"})
		self.assertEqual(frappe.local.response.get("code"), "UPSTREAM_ERROR")
		self.assertEqual(frappe.get_all("HISI Surveillance Session", filters={"session_no": session_no}, pluck="status")[0], "active")

	def test_replayed_checkin_timestamp_rejected(self):
		code = self.issue_code()
		frappe.set_user(self.officer)
		surveillance.start_session(mandate_code=code, location=checkin(minutes_ago=10), device={"app_version": "1.4.2", "user_agent": "test-agent"})
		self.assertEqual(frappe.local.response.get("code"), "LOCATION_CHECK_FAILED")
		self.assertEqual(frappe.db.count("HISI Surveillance Session"), 0)

	def test_out_of_scope_never_reaches_upstream(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		with patch.object(patient.requests, "get") as mock_get:
			frappe.set_user(self.officer)
			surveillance.surveil_patient(session_id=session_no, steps=["patient"], filters={"identification_type": "CR ID", "identification_number": "CR2473157549999-9"})
		self.assertEqual(frappe.local.response.get("code"), "FORBIDDEN")
		self.assertEqual(mock_get.call_count, 0)

	def test_unresolvable_in_scope_identifier_not_found(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		empty = http_response({"status": 200, "message": "Client successfully fetched", "count": 0, "data": []})
		with patch.object(patient.requests, "get", return_value=empty):
			frappe.set_user(self.officer)
			surveillance.surveil_patient(session_id=session_no, steps=["patient"], filters={"identification_type": "National ID", "identification_number": "36637305"})
		self.assertEqual(frappe.local.response.get("code"), "NOT_FOUND")

	def test_audit_export_carries_surveiled_targets(self):
		practitioner = self.make_practitioner("Dr. A. Wanjiku", self.facility)
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		frappe.set_user(self.dispatcher)
		surveillance.get_session_audit(session_id=session_no)
		data = frappe.local.response["data"]
		targets = data["surveiled_targets"]
		self.assertEqual(targets["facility"]["name"], "Nairobi East Clinic")
		self.assertEqual([p["record_id"] for p in targets["practitioners"]], [practitioner])


class TestFacilitySearch(SurveillanceTestCase):
	"""Mandate dispatch facility lookup: DHA register via HISI Settings."""

	def _search(self, term):
		surveillance.search_facilities(term=term)

	def test_search_requires_term(self):
		frappe.set_user(self.dispatcher)
		self._search("")
		self.assertEqual(frappe.local.response.get("code"), "BAD_REQUEST")

	def test_search_maps_fields_and_query_shape(self):
		payload = [
			{
				"facility_name": "Nairobi Level 5 Hospital",
				"facility_code": "KE-N5-0001",
				"facility_fid": "FID-N5-1",
				"operational_status": "Active",
				"latitude": -1.2921,
				"longitude": 36.7767,
			},
			{"facility_name": "Nairobi East Clinic", "facility_code": "KE-NEC-0001"},
			{"not_a_facility": True},
		]
		with patch("hisi.api.surveillance.facilities.http_get", return_value=payload) as mocked:
			frappe.set_user(self.dispatcher)
			self._search("nairobi")
		self.assertEqual(frappe.local.response.get("status"), "success")
		data = frappe.local.response["data"]
		self.assertEqual(data["total"], 2)
		self.assertEqual(data["facilities"][0]["facility_name"], "Nairobi Level 5 Hospital")
		self.assertEqual(data["facilities"][0]["facility_fid"], "FID-N5-1")
		self.assertEqual(data["facilities"][1]["latitude"], None)
		(url, params) = mocked.call_args[0]
		self.assertEqual(url, "https://uat.dha.go.ke/v1/hwr/resource/Health Facility")
		self.assertEqual(params["filters"], '[["facility_name", "like", "%nairobi%"]]')
		self.assertEqual(params["order_by"], "facility_name asc")
		self.assertEqual(params["limit_page_length"], 20)
		self.assertEqual(params["limit_start"], 0)

	def test_search_uses_production_base_when_simulation_off(self):
		self.set_settings(enable_uat_simulation=0)
		with patch("hisi.api.surveillance.facilities.http_get", return_value=[]) as mocked:
			frappe.set_user(self.dispatcher)
			self._search("nairobi")
		self.assertEqual(mocked.call_args[0][0], "https://api.dha.go.ke/v1/hwr/resource/Health Facility")

	def test_search_unconfigured_path_bad_request(self):
		self.set_settings(fetch_facility="")
		frappe.set_user(self.dispatcher)
		self._search("nairobi")
		self.assertEqual(frappe.local.response.get("code"), "BAD_REQUEST")

	def test_search_requires_payer_membership(self):
		frappe.set_user(self.officer)
		with patch("hisi.api.surveillance.facilities.http_get") as mocked:
			self._search("nairobi")
		self.assertEqual(frappe.local.response.get("code"), "FORBIDDEN")
		self.assertFalse(mocked.called)

	def test_search_writes_facilities_searched_event(self):
		frappe.set_user(self.dispatcher)
		before = self.event_count()
		with patch("hisi.api.surveillance.facilities.http_get", return_value=[]):
			self._search("mombasa")
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "facilities_searched")
		self.assertEqual(events[0]["target_type"], "facility")
		self.assertIsNone(events[0]["session"])


class TestMultiCompanySharedTarget(SurveillanceTestCase):
	"""Two payer companies surveilling the same facility / its practitioners:
	the target registry is shared; mandates, sessions and audit stay per-company."""

	def test_second_company_issues_on_same_facility_row(self):
		self.issue_mandate()
		frappe.set_user(self.dispatcher_b)
		surveillance.issue_mandate(
			facility=self.facility,
			assigned_officer=self.officer,
			reason="Suspicion on claim CLM-2026-0442",
			scope_lines=[{"line_type": "patient", "patient_identifier": "36637305"}],
			valid_from=valid_window()[0],
			expires_at=valid_window()[1],
		)
		self.assertEqual(frappe.local.response.get("status"), "success")
		rows = frappe.get_all("HISI Surveillance Mandate", fields=["company", "facility"])
		self.assertEqual(sorted([r["company"] for r in rows]), sorted([self.org, self.org_b]))
		# one shared facility row - no per-company duplicate
		self.assertEqual(frappe.db.count("HISI Health Facility", filters={"name": self.facility}), 1)

	def test_cross_company_session_and_audit_wall(self):
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		# Beta cannot audit or list Acme sessions
		frappe.set_user(self.dispatcher_b)
		surveillance.get_session_audit(session_id=session_no)
		self.assertEqual(frappe.local.response.get("code"), "FORBIDDEN")
		surveillance.list_sessions()
		self.assertEqual(frappe.local.response["data"]["total"], 0)
		# Acme sees its own
		frappe.set_user(self.dispatcher)
		surveillance.list_sessions()
		self.assertEqual(frappe.local.response["data"]["total"], 1)
		surveillance.get_session_audit(session_id=session_no)
		self.assertEqual(frappe.local.response.get("status"), "success")

	def test_non_member_listing_blocked(self):
		frappe.set_user(self.officer)  # not a member of any company
		surveillance.list_sessions()
		self.assertEqual(frappe.local.response.get("code"), "FORBIDDEN")
		surveillance.list_mandates()
		self.assertEqual(frappe.local.response.get("code"), "FORBIDDEN")

	def test_shared_practitioner_row_across_companies(self):
		self.set_settings(fetch_practitioner="/v1/hwr/resource")
		registry_rows = [
			{
				"name": "Dr. A. Wanjiku",
				"cadre": "Medical Officer",
				"regulator": "KMPDC",
				"registration_number": "KMPDC/4471",
				"facility_name": self.facility,
			}
		]
		code = self.issue_code()
		self.start_session(code)
		session_no = frappe.local.response["data"]["session_no"]
		frappe.set_user(self.officer)
		with patch("hisi.api.surveillance.api.http_get", return_value=registry_rows) as mocked:
			surveillance.list_practitioners(session_id=session_no)
		self.assertEqual(frappe.local.response.get("status"), "success")
		self.assertEqual([p["record_id"] for p in frappe.local.response["data"]["practitioners"]], ["Dr. A. Wanjiku"])
		self.assertTrue(mocked.called)
		# Beta session on the same facility: local row exists, registry not consulted
		frappe.set_user(self.dispatcher_b)
		surveillance.issue_mandate(
			facility=self.facility,
			assigned_officer=self.officer,
			reason="x",
			scope_lines=[{"line_type": "patient", "patient_identifier": "36637305"}],
			valid_from=valid_window()[0],
			expires_at=valid_window()[1],
		)
		code_b = frappe.local.response["data"]["short_code"]
		frappe.set_user(self.officer)
		self.start_session(code_b)
		session_no_b = frappe.local.response["data"]["session_no"]
		with patch("hisi.api.surveillance.api.http_get") as mocked2:
			surveillance.list_practitioners(session_id=session_no_b)
		self.assertEqual([p["record_id"] for p in frappe.local.response["data"]["practitioners"]], ["Dr. A. Wanjiku"])
		self.assertFalse(mocked2.called)
		self.assertEqual(frappe.db.count("HISI Professional Record"), 1)
