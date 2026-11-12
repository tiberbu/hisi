import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from hisi.api import patient


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
                "id": 2413479,
                "first_name": "Jane",
                "middle_name": "Aoko",
                "last_name": "Doe",
                "gender": "FEMALE",
                "msisdn": "0724512355",
                "date_of_birth": "1993-11-06",
                "cr_id": "CR2473157541615-8",
                "client_status": "Active",
                "identification_type": "CR ID",
                "identification_number": "CR2473157541615-8",
            }
        ],
    }


def consent_payload():
    return {
        "data": [
            {
                "name": "VCR-20261028-8DCBBCFF",
                "client": "CR2473157541615-8",
                "principal_cr_id": "CR2473157541615-8",
                "status": "Active",
                "consent_status": "Approved",
                "channel": "OTP",
                "verification_status": "Verified",
                "verified_by": "CR2473157541615-8",
                "verification_timestamp": "2026-10-28 18:25:27",
                "facility_id": "FID-47-105963-0",
                "requesting_system": "HALCYON HEALTHCARE LIMITED",
                "visit_id": "b92d1e8f-accc-48d4-84dd-2eb3e0d9fdca",
                "visit_request_id": "74784F3318C5",
                "creation": "2026-10-28 19:02:11",
            }
        ]
    }


def claim_payload():
    return {
        "data": [
            {
                "name": "59f53f79-b25f-4994-8e40-4a94bf1cf667",
                "client": "CR2473157541615-8",
                "client_name": "Jane Aoko Doe",
                "facility": "FID-47-105963-0",
                "creation": "2026-10-28 19:02:11",
                "claim_status": "Paid",
                "interventions": ["Consultation", "Lab tests"],
                "interventions_code": ["A01", "B02"],
                "date_start": "2026-10-28",
                "date_end": "2026-10-29",
                "claim_subtype": "Inpatient",
                "claim_amount": "KES 134,400",
                "diagnoses_code": "A1B.C",
                "diagnoses": "Pneumonia",
                "facility_name": "Halcyon Hospital",
                "use": "inpatient",
            }
        ]
    }


def dha_eligibility_payload():
    return {
        "requestIdType": 3,
        "requestIdNumber": "CR6839901180428-3",
        "memberCrNumber": "CR6839901180428-3",
        "fullName": "IRAD MWENDO",
        "statusCode": "10",
        "statusDesc": "Member found. Check Schemes for coverages",
        "schemes": [
            {
                "schemeName": "UHC",
                "schemeId": 1,
                "memberType": "PRIMARY",
                "coverage": {"status": "1", "message": "The individual is covered.", "reason": "Payment is up to Date."},
            },
            {
                "schemeName": "SHIF",
                "schemeId": 2,
                "memberType": "PRIMARY",
                "coverage": {"status": "1", "message": "The individual is covered.", "reason": "Payment is up to Date."},
            },
        ],
    }


def b64url_decode(segment):
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


class TestPatientFlow(IntegrationTestCase):
    def set_settings(self, **values):
        doc = frappe.get_doc("HISI Settings")
        for key, value in values.items():
            doc.set(key, value)
        doc.save(ignore_permissions=True)

    def setUp(self):
        self.set_settings(
            base_url="https://api.dha.go.ke",
            fetch_client="/client-registry/fetch-client",
            eligibility_check="/backend/api/v5/coverage/validate",
            fetch_patient_consent="/v1/cmp/resource/Visit Consent",
            consumer_key="KEY",
            consumer_secret="SECRET",
            username="USER",
            password="PASS",
            upstream_api_timeout=60,
            jwt_ttl_s=60,
            uat_base_url="https://uat.dha.go.ke",
            enable_uat_simulation=1,
            data_source="API",
            mpi_id_resolution="",
            patient_resources=[],
        )

    def run_flow(self, steps, filters=None):
        patient.patient_flow(data=json.dumps({"steps": steps, "filters": filters or {}}))

    def test_full_sequence_flow(self):
        elig = http_response({"message": {"status": "Active", "scheme": "SHIF"}})
        consent = http_response(consent_payload())
        with patch.object(patient.requests, "get", side_effect=[http_response(patient_payload()), consent]) as mock_get, patch.object(
            patient.requests, "post", return_value=elig
        ) as mock_post:
            self.run_flow(["patient", "eligibility", "consent"], {"identification_type": "CR ID", "identification_number": "CR2473157541615-8"})
        self.assertEqual(mock_get.call_count, 2)
        patient_call, consent_call = mock_get.call_args_list
        self.assertEqual(patient_call.args[0], "https://uat.dha.go.ke/client-registry/fetch-client")
        patient_params = patient_call.kwargs["params"]
        self.assertEqual(
            json.loads(patient_params["payload"]),
            {"identification_type": "CR ID", "identification_number": "CR2473157541615-8"},
        )
        self.assertIn("Authorization", patient_call.kwargs["headers"])
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(mock_post.call_args.args[0], "https://uat.dha.go.ke/backend/api/v5/coverage/validate")
        self.assertEqual(
            mock_post.call_args.kwargs["json"],
            {"requestIdType": 3, "requestIdNumber": "CR2473157541615-8"},
        )
        self.assertIn("Authorization", mock_post.call_args.kwargs["headers"])
        self.assertEqual(consent_call.args[0], "https://uat.dha.go.ke/v1/cmp/resource/Visit Consent")
        self.assertNotIn("Authorization", consent_call.kwargs["headers"])
        consent_params = consent_call.kwargs["params"]
        self.assertEqual(json.loads(consent_params["filters"]), [["client", "=", "2413479"]])
        self.assertEqual(json.loads(consent_params["fields"]), patient.CONSENT_FIELDS)
        response = frappe.local.response
        self.assertEqual(response.get("status"), "success")
        self.assertEqual(response.get("http_status_code"), 200)
        data = response.get("data")
        self.assertEqual(data["cr_id"], "2413479")
        self.assertEqual(data["steps"], ["patient", "eligibility", "consent"])
        self.assertEqual(data["results"]["patient"]["cr_id"], "2413479")
        self.assertEqual(data["results"]["patient"]["name"], "Jane Doe")
        self.assertEqual(data["results"]["patient"]["msisdn"], "0724512355")
        self.assertTrue(data["results"]["eligibility"]["eligible"])
        self.assertEqual(data["results"]["eligibility"]["scheme"], "SHIF")
        self.assertEqual(len(data["results"]["consent"]["records"]), 1)
        self.assertEqual(data["results"]["consent"]["records"][0]["consent_status"], "Approved")
        self.assertNotIn("base_url", data["results"]["consent"])

    def test_jwt_matches_dha_contract(self):
        with patch.object(patient.requests, "get", return_value=http_response(patient_payload())) as mock_get:
            self.run_flow(["patient"], {"identification_type": "CR ID", "identification_number": "CR2473157541615-8"})
        token = mock_get.call_args.kwargs["headers"]["Authorization"].split(" ", 1)[1]
        header_segment, payload_segment, signature = token.split(".")
        header = json.loads(b64url_decode(header_segment))
        payload = json.loads(b64url_decode(payload_segment))
        self.assertEqual(header, {"alg": "HS256", "typ": "JWT"})
        self.assertEqual(payload["key"], "KEY")
        self.assertEqual(payload["exp"] - payload["iat"], 60)
        signing_input = "{}.{}".format(header_segment, payload_segment).encode("utf-8")
        expected = patient.base64url_encode(hmac.new(b"SECRET", signing_input, hashlib.sha256).digest())
        self.assertEqual(signature, expected)

    def test_jwt_ttl_from_settings(self):
        self.set_settings(jwt_ttl_s=120)
        with patch.object(patient.requests, "get", return_value=http_response(patient_payload())) as mock_get:
            self.run_flow(["patient"], {"identification_type": "CR ID", "identification_number": "CR1"})
        token = mock_get.call_args.kwargs["headers"]["Authorization"].split(" ", 1)[1]
        payload = json.loads(b64url_decode(token.split(".")[1]))
        self.assertEqual(payload["exp"] - payload["iat"], 120)

    def test_eligibility_only_with_cr_id_filter(self):
        with patch.object(patient.requests, "post", return_value=http_response({"status": "Active"})) as mock_post:
            self.run_flow(["eligibility"], {"cr_id": "CR1"})
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(mock_post.call_args.args[0], "https://uat.dha.go.ke/backend/api/v5/coverage/validate")
        self.assertEqual(mock_post.call_args.kwargs["json"], {"requestIdType": 3, "requestIdNumber": "CR1"})
        self.assertEqual(frappe.local.response.get("data")["cr_id"], "CR1")
        self.assertNotIn("patient", frappe.local.response.get("data")["results"])

    def test_eligibility_explicit_request_id_filters(self):
        with patch.object(patient.requests, "post", return_value=http_response({"status": "Active"})) as mock_post:
            self.run_flow(["eligibility"], {"cr_id": "CR1", "requestIdType": 1, "requestIdNumber": "45887531"})
        self.assertEqual(mock_post.call_args.kwargs["json"], {"requestIdType": 1, "requestIdNumber": "45887531"})

    def test_eligibility_uses_original_identifier_when_patient_lacks_cr_id(self):
        payload = {
            "status": 200,
            "message": "Client successfully fetched",
            "count": 1,
            "data": [
                {
                    "id": 2413479,
                    "first_name": "Jane",
                    "middle_name": None,
                    "last_name": "Doe",
                    "gender": "FEMALE",
                    "msisdn": "0724512355",
                    "date_of_birth": "1993-11-06",
                    "client_status": "Active",
                    "identification_type": "CR ID",
                    "identification_number": "CR6839901180428-3",
                }
            ],
        }
        elig = http_response({"message": {"status": "Active", "scheme": "SHIF"}})
        with patch.object(patient.requests, "get", return_value=http_response(payload)), patch.object(
            patient.requests, "post", return_value=elig
        ) as mock_post:
            self.run_flow(["patient", "eligibility"], {"identification_type": "CR ID", "identification_number": "CR6839901180428-3"})
        self.assertEqual(mock_post.call_args.kwargs["json"], {"requestIdType": 3, "requestIdNumber": "CR6839901180428-3"})
        data = frappe.local.response.get("data")
        self.assertEqual(data["cr_id"], "2413479")
        self.assertEqual(data["results"]["patient"]["cr_id"], "2413479")

    def test_eligibility_unknown_identification_type_fails(self):
        with patch.object(patient.requests, "get", return_value=http_response(patient_payload())), patch.object(
            patient.requests, "post"
        ) as mock_post:
            self.run_flow(["patient", "eligibility"], {"identification_type": "National ID", "identification_number": "36637305"})
        response = frappe.local.response
        self.assertEqual(response.get("code"), "BAD_REQUEST")
        self.assertIn("requestIdType", response.get("message"))
        self.assertEqual(mock_post.call_count, 0)

    def test_eligibility_parses_dha_schemes(self):
        with patch.object(patient.requests, "post", return_value=http_response(dha_eligibility_payload())):
            self.run_flow(["eligibility"], {"cr_id": "CR6839901180428-3"})
        data = frappe.local.response.get("data")
        self.assertTrue(data["results"]["eligibility"]["eligible"])
        self.assertEqual(data["results"]["eligibility"]["status"], "Member found. Check Schemes for coverages")
        self.assertEqual(data["results"]["eligibility"]["scheme"], "UHC")
        schemes = data["results"]["eligibility"]["schemes"]
        self.assertEqual(len(schemes), 2)
        self.assertEqual(schemes[0]["scheme"], "UHC")
        self.assertTrue(schemes[0]["covered"])
        self.assertTrue(schemes[1]["covered"])

    def test_eligibility_lapsed_schemes_not_eligible(self):
        payload = dha_eligibility_payload()
        for scheme in payload["schemes"]:
            scheme["coverage"]["status"] = "0"
            scheme["coverage"]["message"] = "The individual is not covered."
        with patch.object(patient.requests, "post", return_value=http_response(payload)):
            self.run_flow(["eligibility"], {"cr_id": "CR6839901180428-3"})
        self.assertFalse(frappe.local.response.get("data")["results"]["eligibility"]["eligible"])

    def test_patient_step_with_cr_id_filter(self):
        with patch.object(patient.requests, "get", return_value=http_response(patient_payload())) as mock_get:
            self.run_flow(["patient"], {"cr_id": "CR6839901180428-3"})
        patient_params = mock_get.call_args.kwargs["params"]
        self.assertEqual(json.loads(patient_params["payload"]), {"id": "CR6839901180428-3"})
        self.assertEqual(frappe.local.response.get("data")["results"]["patient"]["cr_id"], "2413479")

    def test_full_flow_with_cr_id_filter(self):
        elig = http_response(dha_eligibility_payload())
        consent = http_response(consent_payload())
        with patch.object(patient.requests, "get", side_effect=[http_response(patient_payload()), consent]) as mock_get, patch.object(
            patient.requests, "post", return_value=elig
        ) as mock_post:
            self.run_flow(["patient", "eligibility", "consent"], {"cr_id": "CR6839901180428-3"})
        patient_params = mock_get.call_args_list[0].kwargs["params"]
        self.assertEqual(json.loads(patient_params["payload"]), {"id": "CR6839901180428-3"})
        self.assertEqual(mock_post.call_args.kwargs["json"], {"requestIdType": 3, "requestIdNumber": "2413479"})
        consent_params = mock_get.call_args_list[1].kwargs["params"]
        self.assertEqual(json.loads(consent_params["filters"]), [["client", "=", "2413479"]])

    def test_accepts_native_json_data(self):
        with patch.object(patient.requests, "get", return_value=http_response(patient_payload())):
            patient.patient_flow(data={"steps": ["patient"], "filters": {"identification_type": "CR ID", "identification_number": "CR2473157541615-8"}})
        self.assertEqual(frappe.local.response.get("status"), "success")
        self.assertEqual(frappe.local.response.get("data")["cr_id"], "2413479")

    def test_consent_only_with_cr_id_filter(self):
        with patch.object(patient.requests, "get", return_value=http_response(consent_payload())) as mock_get:
            self.run_flow(["consent"], {"cr_id": "CR1"})
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(mock_get.call_args.args[0], "https://uat.dha.go.ke/v1/cmp/resource/Visit Consent")
        self.assertNotIn("Authorization", mock_get.call_args.kwargs["headers"])

    def test_consent_records_without_visit_id_filtered(self):
        record_without_visit = dict(consent_payload()["data"][0])
        record_without_visit["name"] = "VCR-20261028-NOVISIT"
        record_without_visit["visit_id"] = ""
        payload = {"data": [consent_payload()["data"][0], record_without_visit]}
        with patch.object(patient.requests, "get", return_value=http_response(payload)):
            self.run_flow(["consent"], {"cr_id": "CR1"})
        records = frappe.local.response.get("data")["results"]["consent"]["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["visit_id"], "b92d1e8f-accc-48d4-84dd-2eb3e0d9fdca")

    def test_consent_requires_uat_base_url(self):
        # A consent config error is isolated to the consent step; the flow
        # still succeeds with the failure recorded per-step.
        self.set_settings(uat_base_url="")
        self.run_flow(["consent"], {"cr_id": "CR1"})
        response = frappe.local.response
        self.assertEqual(response.get("status"), "success")
        data = response.get("data")
        self.assertNotIn("consent", data["results"])
        self.assertIn("uat_base_url", data["errors"]["consent"])

    def test_consent_requires_fetch_patient_consent(self):
        self.set_settings(fetch_patient_consent="")
        self.run_flow(["consent"], {"cr_id": "CR1"})
        response = frappe.local.response
        self.assertEqual(response.get("status"), "success")
        data = response.get("data")
        self.assertNotIn("consent", data["results"])
        self.assertIn("fetch_patient_consent", data["errors"]["consent"])

    def test_no_patient_step_requires_cr_id(self):
        self.run_flow(["eligibility"], {})
        response = frappe.local.response
        self.assertEqual(response.get("code"), "BAD_REQUEST")
        self.assertEqual(response.get("http_status_code"), 400)

    def test_unknown_step_rejected(self):
        self.run_flow(["patient", "billing"], {})
        response = frappe.local.response
        self.assertEqual(response.get("code"), "BAD_REQUEST")

    def test_empty_steps_rejected(self):
        self.run_flow([])
        self.assertEqual(frappe.local.response.get("code"), "BAD_REQUEST")

    def test_invalid_json_rejected(self):
        patient.patient_flow(data="{not json")
        self.assertEqual(frappe.local.response.get("code"), "BAD_REQUEST")

    def test_duplicate_steps_deduped(self):
        with patch.object(patient.requests, "get", return_value=http_response(patient_payload())) as mock_get:
            self.run_flow(["patient", "patient"], {"identification_type": "CR ID", "identification_number": "CR1"})
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(frappe.local.response.get("data")["steps"], ["patient"])

    def test_patient_not_found(self):
        with patch.object(patient.requests, "get", return_value=http_response({"message": "Client successfully fetched", "count": 0, "data": []})):
            self.run_flow(["patient"], {"identification_type": "CR ID", "identification_number": "CR1"})
        response = frappe.local.response
        self.assertEqual(response.get("code"), "NOT_FOUND")
        self.assertEqual(response.get("http_status_code"), 404)

    def test_ineligible_still_success(self):
        elig = http_response({"message": {"status": "Inactive", "scheme": "SHIF"}})
        with patch.object(patient.requests, "get", return_value=http_response(patient_payload())), patch.object(
            patient.requests, "post", return_value=elig
        ):
            self.run_flow(["patient", "eligibility"], {"identification_type": "CR ID", "identification_number": "CR2473157541615-8"})
        response = frappe.local.response
        self.assertEqual(response.get("status"), "success")
        self.assertFalse(response.get("data")["results"]["eligibility"]["eligible"])
        self.assertEqual(response.get("data")["results"]["eligibility"]["status"], "Inactive")

    def test_simulation_prefers_uat_base_url_setting(self):
        self.set_settings(uat_base_url="https://uat2.dha.go.ke")
        with patch.object(patient.requests, "get", return_value=http_response(consent_payload())) as mock_get:
            self.run_flow(["consent"], {"cr_id": "CR1"})
        # The consent call goes to the configured UAT origin, unauthenticated;
        # the origin is no longer echoed in the response.
        self.assertTrue(mock_get.call_args.args[0].startswith("https://uat2.dha.go.ke"))
        self.assertNotIn("Authorization", mock_get.call_args.kwargs["headers"])
        self.assertNotIn("base_url", frappe.local.response.get("data")["results"]["consent"])

    def test_consent_without_simulation_uses_base_url_with_jwt(self):
        self.set_settings(enable_uat_simulation=0)
        with patch.object(patient.requests, "get", return_value=http_response(consent_payload())) as mock_get:
            self.run_flow(["consent"], {"cr_id": "CR1"})
        # With simulation off, consent runs against the production origin with
        # the usual JWT bearer.
        self.assertEqual(mock_get.call_args.args[0], "https://api.dha.go.ke/v1/cmp/resource/Visit Consent")
        self.assertTrue(mock_get.call_args.kwargs["headers"]["Authorization"].startswith("Bearer "))
        self.assertNotIn("base_url", frappe.local.response.get("data")["results"]["consent"])

    def test_consent_fields_include_creation(self):
        with patch.object(patient.requests, "get", return_value=http_response(consent_payload())) as mock_get:
            self.run_flow(["consent"], {"cr_id": "CR1"})
        self.assertIn("creation", json.loads(mock_get.call_args.kwargs["params"]["fields"]))

    def test_claims_step_uses_med_claim_search_with_jwt(self):
        with patch.object(patient.requests, "get", return_value=http_response(claim_payload())) as mock_get:
            self.run_flow(["claims"], {"cr_id": "CR1"})
        call = mock_get.call_args_list[0]
        # The claims step follows the active origin — UAT under simulation
        # mode here — with the JWT bearer, on the configured med claim path.
        self.assertEqual(call.args[0], "https://uat.dha.go.ke/v1/med/resource/Claim Record")
        self.assertEqual(json.loads(call.kwargs["params"]["filters"]), [["client", "=", "CR1"]])
        self.assertEqual(json.loads(call.kwargs["params"]["fields"]), patient.CLAIM_FIELDS)
        self.assertTrue(call.kwargs["headers"]["Authorization"].startswith("Bearer "))
        records = frappe.local.response.get("data")["results"]["claims"]["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["claim_status"], "Paid")

    def test_claims_records_carry_activity_from_version_history(self):
        claim = claim_payload()["data"][0]
        versions = http_response(
            {
                "data": [
                    {
                        "docname": claim["name"],
                        "creation": "2026-10-29 08:00:00",
                        "data": json.dumps({"added": [], "changed": [["claim_status", "received", "manual_review"]], "removed": []}),
                    },
                    {
                        "docname": claim["name"],
                        "creation": "2026-11-02 10:33:14",
                        "data": json.dumps({"added": [], "changed": [["claim_status", "manual_review", "approved"]], "removed": []}),
                    },
                ]
            }
        )
        with patch.object(patient.requests, "get", side_effect=[http_response(claim_payload()), versions]) as mock_get:
            self.run_flow(["claims"], {"cr_id": "CR1"})
        version_call = mock_get.call_args_list[1]
        # One batched Version request on the claim lane's Version route,
        # keyed by every claim name via the registry's `in` filter.
        self.assertEqual(version_call.args[0], "https://uat.dha.go.ke/v1/med/resource/Version")
        self.assertEqual(
            json.loads(version_call.kwargs["params"]["filters"]),
            [["ref_doctype", "=", "Claim Record"], ["docname", "in", [claim["name"]]]],
        )
        self.assertEqual(version_call.kwargs["params"]["order_by"], "creation asc")
        record = frappe.local.response.get("data")["results"]["claims"]["records"][0]
        self.assertEqual(len(record["activity"]), 2)
        self.assertEqual(record["activity"][0]["at"], "2026-10-29 08:00:00")
        self.assertEqual(record["activity"][0]["changes"][0], {"field": "claim_status", "from": "received", "to": "manual_review"})
        self.assertEqual(record["activity"][1]["changes"][0]["to"], "approved")

    def test_claims_activity_fetch_failure_degrades_to_creation_only(self):
        with patch.object(
            patient.requests,
            "get",
            side_effect=[http_response(claim_payload()), http_response(None, status_code=400)],
        ):
            self.run_flow(["claims"], {"cr_id": "CR1"})
        response = frappe.local.response
        self.assertEqual(response.get("status"), "success")
        self.assertEqual(response.get("data")["errors"], {})
        record = response.get("data")["results"]["claims"]["records"][0]
        # The Version lookup 400s (not retried) — the step still succeeds
        # and the record keeps an empty activity (the UI shows the sent
        # point alone).
        self.assertEqual(record["activity"], [])

    def test_preauth_records_carry_activity_keyed_by_bundle_id(self):
        payload = claim_payload()
        payload["data"][0]["bundle_id"] = "bundle-1"
        versions = http_response(
            {
                "data": [
                    {
                        "docname": "bundle-1",
                        "creation": "2026-11-10 11:23:31",
                        "data": json.dumps(
                            {
                                "added": [],
                                "changed": [
                                    ["practitioner_approval_status", "Pending Consultant Review", "Approved by Consultant"],
                                    ["comment", None, "Approved"],
                                ],
                                "removed": [],
                            }
                        ),
                    }
                ]
            }
        )
        with patch.object(
            patient.requests,
            "get",
            side_effect=[http_response(payload), http_response({"data": []}), versions],
        ) as mock_get:
            self.run_flow(["preauth"], {"cr_id": "CR1"})
        version_call = mock_get.call_args_list[2]
        self.assertEqual(version_call.args[0], "https://uat.dha.go.ke/v1/preauth/resource/Version")
        self.assertEqual(
            json.loads(version_call.kwargs["params"]["filters"]),
            [["ref_doctype", "=", "Shared Health Record"], ["docname", "in", ["bundle-1"]]],
        )
        record = frappe.local.response.get("data")["results"]["preauth"]["records"][0]
        self.assertEqual(len(record["activity"]), 1)
        self.assertEqual(
            record["activity"][0]["changes"][0],
            {"field": "practitioner_approval_status", "from": "Pending Consultant Review", "to": "Approved by Consultant"},
        )
        # A null old value (the comment was added, not changed) parses to
        # from=None, never an empty-string surprise.
        self.assertEqual(record["activity"][0]["changes"][1], {"field": "comment", "from": None, "to": "Approved"})

    def test_preauth_linked_procedures_carry_activity(self):
        payload = claim_payload()
        procedure = {
            "name": "9000proc-a1b2",
            "procedure_fulfil_status": "Active Procedure",
            "estimated_procedure_start_date": "2026-06-17",
            "estimated_procedure_end_date": "2026-06-17",
            "practitioner": "FID-47-105963-0",
            "practitioner_name": "Dr. Siranjofu",
        }
        proc_versions = http_response(
            {
                "data": [
                    {
                        "docname": procedure["name"],
                        "creation": "2026-06-17 14:57:23",
                        "data": json.dumps(
                            {
                                "added": [],
                                "changed": [["procedure_fulfil_status", "Upcoming Procedure", "Active Procedure"]],
                                "removed": [],
                            }
                        ),
                    }
                ]
            }
        )
        with patch.object(
            patient.requests,
            "get",
            side_effect=[http_response(payload), http_response({"data": [procedure]}), proc_versions],
        ) as mock_get:
            self.run_flow(["preauth"], {"cr_id": "CR1"})
        version_call = mock_get.call_args_list[2]
        # One batched Version request keyed by the procedure name (the
        # registry's Procedure Record doctype on the preauth route).
        self.assertEqual(version_call.args[0], "https://uat.dha.go.ke/v1/preauth/resource/Version")
        self.assertEqual(
            json.loads(version_call.kwargs["params"]["filters"]),
            [["ref_doctype", "=", "Procedure Record"], ["docname", "in", [procedure["name"]]]],
        )
        record = frappe.local.response.get("data")["results"]["preauth"]["records"][0]
        self.assertEqual(len(record["procedures"]), 1)
        self.assertEqual(len(record["procedures"][0]["activity"]), 1)
        self.assertEqual(
            record["procedures"][0]["activity"][0]["changes"][0],
            {"field": "procedure_fulfil_status", "from": "Upcoming Procedure", "to": "Active Procedure"},
        )

    def test_preauth_step_uses_med_preauth_search_with_jwt(self):
        with patch.object(patient.requests, "get", side_effect=[http_response(claim_payload()), http_response({"data": []})]) as mock_get:
            self.run_flow(["preauth"], {"cr_id": "CR1"})
        call = mock_get.call_args_list[0]
        self.assertEqual(call.args[0], "https://uat.dha.go.ke/v1/preauth/resource/Claim Record")
        self.assertEqual(json.loads(call.kwargs["params"]["filters"]), [["client", "=", "CR1"]])
        self.assertTrue(call.kwargs["headers"]["Authorization"].startswith("Bearer "))
        records = frappe.local.response.get("data")["results"]["preauth"]["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["procedures"], [])

    def test_preauth_records_include_linked_procedures(self):
        procedures = http_response(
            {
                "data": [
                    {
                        "name": "59f53f79-proc-1",
                        "procedure_fulfil_status": "Scheduled",
                        "estimated_procedure_start_date": "2026-11-02",
                        "estimated_procedure_end_date": "2026-11-03",
                        "practitioner": "FID-47-105963-0",
                        "practitioner_name": "Dr. Aoko",
                    }
                ]
            }
        )
        with patch.object(
            patient.requests,
            "get",
            side_effect=[http_response(claim_payload()), procedures, http_response({"data": []})],
        ) as mock_get:
            self.run_flow(["preauth"], {"cr_id": "CR1"})
        procedure_call = mock_get.call_args_list[1]
        # The procedure lookup targets the configured preauth procedure path,
        # keyed by the preauth record's claim id (its document name).
        self.assertEqual(procedure_call.args[0], "https://uat.dha.go.ke/v1/preauth/resource/Procedure Record")
        self.assertEqual(json.loads(procedure_call.kwargs["params"]["filters"]), [["claim_id", "=", "59f53f79-b25f-4994-8e40-4a94bf1cf667"]])
        self.assertEqual(json.loads(procedure_call.kwargs["params"]["fields"]), patient.PROCEDURE_FIELDS)
        record = frappe.local.response.get("data")["results"]["preauth"]["records"][0]
        self.assertEqual(len(record["procedures"]), 1)
        self.assertEqual(record["procedures"][0]["procedure_fulfil_status"], "Scheduled")
        self.assertEqual(record["procedures"][0]["practitioner_name"], "Dr. Aoko")
        # The third call is the batched procedure Version lookup; with no
        # versions in the registry the procedure's activity stays empty.
        self.assertEqual(mock_get.call_args_list[2].args[0], "https://uat.dha.go.ke/v1/preauth/resource/Version")
        self.assertEqual(record["procedures"][0]["activity"], [])

    def test_database_data_source_not_available_yet(self):
        self.set_settings(data_source="Database")
        self.run_flow(["claims"], {"cr_id": "CR1"})
        response = frappe.local.response
        self.assertEqual(response.get("status"), "error")
        self.assertEqual(response.get("code"), "BAD_REQUEST")
        self.assertIn("data source", response.get("message").lower())

    def test_upstream_http_error(self):
        with patch.object(patient.requests, "get", return_value=http_response(None, status_code=502)):
            self.run_flow(["patient"], {"identification_type": "CR ID", "identification_number": "CR1"})
        response = frappe.local.response
        self.assertEqual(response.get("code"), "UPSTREAM_ERROR")
        self.assertIn("502", response.get("message"))

    def test_upstream_non_json(self):
        response = MagicMock()
        response.status_code = 200
        response.json.side_effect = ValueError("not json")
        with patch.object(patient.requests, "get", return_value=response):
            self.run_flow(["patient"], {"identification_type": "CR ID", "identification_number": "CR1"})
        self.assertEqual(frappe.local.response.get("code"), "UPSTREAM_ERROR")

    def test_failed_step_isolated_per_step(self):
        # A failing claims/preauth step must not sink consent/eligibility:
        # the flow succeeds, the failed steps land in `errors`.
        elig = http_response({"message": {"status": "Active", "scheme": "SHIF"}})
        with patch.object(patient.requests, "get", side_effect=[http_response(consent_payload()), http_response(None, status_code=400), http_response(None, status_code=400)]) as mock_get, patch.object(
            patient.requests, "post", return_value=elig
        ):
            self.run_flow(["consent", "eligibility", "claims", "preauth"], {"cr_id": "CR2473157541615-8"})
        response = frappe.local.response
        self.assertEqual(response.get("status"), "success")
        self.assertEqual(response.get("http_status_code"), 200)
        data = response.get("data")
        self.assertIn("consent", data["results"])
        self.assertIn("eligibility", data["results"])
        self.assertNotIn("claims", data["results"])
        self.assertNotIn("preauth", data["results"])
        self.assertIn("claims", data["errors"])
        self.assertIn("preauth", data["errors"])
        self.assertIn("400", data["errors"]["claims"])
        self.assertIn("400", data["errors"]["preauth"])

    def test_transient_upstream_404_retried_then_succeeds(self):
        # UAT flaps routes to "404 Route Not Found" and back to 200: the
        # first two 404s are retried and the third attempt's 200 wins.
        with patch.object(patient.requests, "get", side_effect=[http_response(None, status_code=404), http_response(None, status_code=404), http_response({"data": []})]) as mock_get:
            self.run_flow(["claims"], {"cr_id": "CR2473157541615-8"})
        response = frappe.local.response
        self.assertEqual(response.get("status"), "success")
        data = response.get("data")
        self.assertEqual(data["results"]["claims"]["records"], [])
        self.assertEqual(data["errors"], {})
        self.assertEqual(mock_get.call_count, 3)

    def test_missing_credentials(self):
        self.set_settings(consumer_key="")
        self.run_flow(["patient"], {"identification_type": "CR ID", "identification_number": "CR1"})
        self.assertEqual(frappe.local.response.get("code"), "BAD_REQUEST")

    def fhir_records_bundle(self):
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": 2,
            "entry": [
                {"resource": {"resourceType": "Encounter", "id": "enc-1", "status": "finished"}},
                {"resource": {"resourceType": "Condition", "id": "cond-1"}},
            ],
        }

    def set_aws_settings(self):
        self.set_settings(
            data_store_id="0123456789abcdef0123456789abcdef01",
            aws_access_key="AKIAEXAMPLEKEY00000001",
            aws_secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            aws_region="eu-west-2",
            query_mapper=[{"param_key": "_include", "param_value": "Encounter:service-provider"}],
        )

    def test_full_sequence_flow_with_records_step(self):
        self.set_aws_settings()
        elig = http_response({"message": {"status": "Active", "scheme": "SHIF"}})
        consent = http_response(consent_payload())
        with patch.object(patient.requests, "get", side_effect=[http_response(patient_payload()), consent, http_response(self.fhir_records_bundle())]) as mock_get, patch.object(
            patient.requests, "post", return_value=elig
        ):
            self.run_flow(["patient", "eligibility", "consent", "records"], {"cr_id": "CR2473157541615-8"})
        records_call = mock_get.call_args_list[2]
        self.assertIn("episode-of-care=b92d1e8f-accc-48d4-84dd-2eb3e0d9fdca", records_call.args[0])
        self.assertIn("_include=Encounter%3Aservice-provider", records_call.args[0])
        data = frappe.local.response.get("data")
        self.assertEqual(data["results"]["records"]["visit_id"], "b92d1e8f-accc-48d4-84dd-2eb3e0d9fdca")
        self.assertEqual([r.get("resourceType") for r in data["results"]["records"]["records"]], ["Encounter", "Condition"])

    def test_records_step_with_visit_id_filter(self):
        self.set_aws_settings()
        with patch.object(patient.requests, "get", return_value=http_response(self.fhir_records_bundle())) as mock_get:
            self.run_flow(["records"], {"visit_id": "d7fd81f8-bb9f-4f5d-8a8e-fc12014aa0d6"})
        self.assertIn("episode-of-care=d7fd81f8-bb9f-4f5d-8a8e-fc12014aa0d6", mock_get.call_args.args[0])
        data = frappe.local.response.get("data")
        self.assertEqual(data["results"]["records"]["visit_id"], "d7fd81f8-bb9f-4f5d-8a8e-fc12014aa0d6")
        self.assertNotIn("consent", data["results"])

    def test_records_step_uses_consent_visit_id(self):
        self.set_aws_settings()
        consent = http_response(consent_payload())
        with patch.object(patient.requests, "get", side_effect=[consent, http_response(self.fhir_records_bundle())]) as mock_get:
            self.run_flow(["consent", "records"], {"cr_id": "CR1"})
        self.assertIn("episode-of-care=b92d1e8f-accc-48d4-84dd-2eb3e0d9fdca", mock_get.call_args_list[1].args[0])
        self.assertEqual(frappe.local.response.get("data")["results"]["records"]["visit_id"], "b92d1e8f-accc-48d4-84dd-2eb3e0d9fdca")

    def test_records_step_fetches_every_comma_listed_visit(self):
        # The lazy timeline sends a records-only call with every consented
        # visit comma-separated: every listed visit is fetched, no cap.
        self.set_aws_settings()
        visits = ["v-1", "v-2", "v-3", "v-4", "v-5", "v-6"]
        with patch.object(patient.requests, "get", return_value=http_response(self.fhir_records_bundle())) as mock_get:
            self.run_flow(["records"], {"visit_id": ",".join(visits)})
        self.assertEqual(mock_get.call_count, len(visits))
        for visit in visits:
            self.assertTrue(any("episode-of-care={}".format(visit) in call.args[0] for call in mock_get.call_args_list))
        data = frappe.local.response.get("data")
        self.assertEqual(data["results"]["records"]["visit_id"], visits[0])
        self.assertEqual(sorted(data["results"]["records"]["by_visit"]), sorted(visits))

    def test_records_step_requires_visit_id(self):
        self.run_flow(["records"], {})
        response = frappe.local.response
        self.assertEqual(response.get("code"), "BAD_REQUEST")
        self.assertIn("visit_id", response.get("message"))

    def mpi_payload(self, shr_id="KE-SHRP-8B626E2B-2B15-4C93-9735"):
        return {
            "status": "success",
            "message": "Success",
            "status_code": 200,
            "data": {
                "patient": {
                    "status": "success",
                    "message": "MPI record found.",
                    "data": {"cr_id": "CR1", "mpi_id": "KE-MPI-D2AB96DC-8971-409A-8DD4-71431E4E88AB", "shr_id": shr_id},
                },
                "facility": None,
                "practitioner": None,
            },
            "pagination": {},
        }

    def set_patient_resource_settings(self):
        self.set_settings(
            mpi_id_resolution="/v1/mpi/patient",
            patient_resources=[{"resource_type": "Observation", "patient_param": "subject"}],
        )

    def test_records_step_merges_and_dedupes_patient_level(self):
        self.set_aws_settings()
        self.set_patient_resource_settings()
        visit = {
            "resourceType": "Bundle",
            "entry": [
                {"resource": {"resourceType": "Encounter", "id": "enc-1"}},
                {"resource": {"resourceType": "Observation", "id": "obs-1"}},
            ],
        }
        observations_by_shr = {
            "resourceType": "Bundle",
            "entry": [
                {"resource": {"resourceType": "Observation", "id": "obs-1"}},
                {"resource": {"resourceType": "Observation", "id": "obs-2"}},
            ],
        }
        observations_by_cr = {
            "resourceType": "Bundle",
            "entry": [
                {"resource": {"resourceType": "Observation", "id": "obs-2"}},
                {"resource": {"resourceType": "Observation", "id": "obs-3"}},
            ],
        }
        with patch.object(
            patient.requests,
            "get",
            side_effect=[
                http_response(visit),
                http_response(self.mpi_payload()),
                http_response(observations_by_shr),
                http_response(observations_by_cr),
            ],
        ) as mock_get:
            self.run_flow(["records"], {"cr_id": "CR1", "visit_id": "v-1"})
        urls = [call.args[0] for call in mock_get.call_args_list]
        self.assertIn("episode-of-care=v-1", urls[0])
        self.assertEqual(urls[1], "https://uat.dha.go.ke/v1/mpi/patient")
        self.assertEqual(mock_get.call_args_list[1].kwargs["params"], {"cr_id": "CR1"})
        self.assertEqual(
            mock_get.call_args_list[1].kwargs["headers"]["Authorization"],
            "Basic " + base64.b64encode(b"USER:PASS").decode("ascii"),
        )
        self.assertTrue(urls[2].endswith("/r4/Observation?subject=KE-SHRP-8B626E2B-2B15-4C93-9735"))
        self.assertTrue(urls[3].endswith("/r4/Observation?subject=CR1"))
        data = frappe.local.response.get("data")
        records = data["results"]["records"]
        self.assertEqual(records["shr_id"], "KE-SHRP-8B626E2B-2B15-4C93-9735")
        self.assertEqual(
            [(r.get("resourceType"), r.get("id")) for r in records["records"]],
            [
                ("Encounter", "enc-1"),
                ("Observation", "obs-1"),
                ("Observation", "obs-2"),
                ("Observation", "obs-3"),
            ],
        )
        self.assertEqual([r.get("id") for r in records["by_resource"]["Observation"]], ["obs-2", "obs-3"])
        self.assertEqual(records["errors"], {})

    def test_records_step_skips_patient_level_without_cr_id(self):
        self.set_aws_settings()
        self.set_patient_resource_settings()
        with patch.object(patient.requests, "get", return_value=http_response(self.fhir_records_bundle())) as mock_get:
            self.run_flow(["records"], {"visit_id": "v-1"})
        urls = [call.args[0] for call in mock_get.call_args_list]
        self.assertEqual(len(urls), 1)
        self.assertIn("episode-of-care=v-1", urls[0])
        data = frappe.local.response.get("data")
        records = data["results"]["records"]
        self.assertEqual(records["shr_id"], "")
        self.assertEqual(records["by_resource"], {})

    def test_records_step_patient_level_without_visit(self):
        self.set_aws_settings()
        self.set_patient_resource_settings()
        observations = {
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "Observation", "id": "obs-1"}}],
        }
        with patch.object(
            patient.requests,
            "get",
            side_effect=[http_response(self.mpi_payload()), http_response(observations), http_response(observations)],
        ) as mock_get:
            self.run_flow(["records"], {"cr_id": "CR1"})
        urls = [call.args[0] for call in mock_get.call_args_list]
        self.assertEqual(urls[0], "https://uat.dha.go.ke/v1/mpi/patient")
        self.assertTrue(urls[1].endswith("/r4/Observation?subject=KE-SHRP-8B626E2B-2B15-4C93-9735"))
        self.assertTrue(urls[2].endswith("/r4/Observation?subject=CR1"))
        data = frappe.local.response.get("data")
        records = data["results"]["records"]
        self.assertEqual(records["visit_id"], "")
        self.assertEqual([r.get("id") for r in records["records"]], ["obs-1"])

    def test_records_step_falls_back_to_cr_id_when_shr_missing(self):
        self.set_aws_settings()
        self.set_patient_resource_settings()
        observations = {
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "Observation", "id": "obs-1"}}],
        }
        with patch.object(
            patient.requests, "get", side_effect=[http_response(self.mpi_payload(shr_id="")), http_response(observations)]
        ) as mock_get:
            self.run_flow(["records"], {"cr_id": "CR1"})
        urls = [call.args[0] for call in mock_get.call_args_list]
        self.assertEqual(urls[0], "https://uat.dha.go.ke/v1/mpi/patient")
        self.assertTrue(urls[1].endswith("/r4/Observation?subject=CR1"))
        data = frappe.local.response.get("data")
        records = data["results"]["records"]
        self.assertEqual(records["shr_id"], "")
        self.assertEqual(records["errors"], {})
        self.assertEqual([r.get("id") for r in records["records"]], ["obs-1"])

    def test_records_step_folds_patient_level_records_into_visit(self):
        # The visit that actually carries FHIR data is not among the
        # consent visits the per-visit fetch covers (the records step caps
        # the visit list). The patient-level records must still reach that
        # visit's by_visit bucket — the journey UI renders visit cards from
        # by_visit only, resolving each record's visit through the
        # Encounter it references.
        self.set_aws_settings()
        self.set_settings(
            mpi_id_resolution="/v1/mpi/patient",
            patient_resources=[{"resource_type": "Condition,Encounter,MedicationRequest", "patient_param": "subject"}],
        )
        empty = {"resourceType": "Bundle", "entry": []}
        condition = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Condition",
                        "id": "cond-1",
                        "encounter": {"reference": "Encounter/enc-1"},
                    }
                }
            ],
        }
        encounter = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Encounter",
                        "id": "enc-1",
                        # upstream sends episodeOfCare as a 0..N array
                        "episodeOfCare": [{"reference": "EpisodeOfCare/v-2"}],
                    }
                }
            ],
        }
        medication = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "MedicationRequest",
                        "id": "med-1",
                        "encounter": {"reference": "Encounter/enc-1"},
                    }
                }
            ],
        }
        with patch.object(
            patient.requests,
            "get",
            side_effect=[
                http_response(consent_payload()),  # consent step
                http_response(empty),  # per-visit fetch for the consent visit (no FHIR data)
                http_response(self.mpi_payload()),  # MPI resolution
                http_response(condition),
                http_response(empty),  # Condition: shr, cr
                http_response(encounter),
                http_response(empty),  # Encounter: shr, cr
                http_response(medication),
                http_response(empty),  # MedicationRequest: shr, cr
            ],
        ):
            self.run_flow(["consent", "records"], {"cr_id": "CR1"})
        data = frappe.local.response.get("data")
        records = data["results"]["records"]
        self.assertEqual(
            [(r.get("resourceType"), r.get("id")) for r in records["by_visit"]["v-2"]],
            [("Condition", "cond-1"), ("Encounter", "enc-1"), ("MedicationRequest", "med-1")],
        )
        # A consent visit without FHIR data keeps an empty bucket (no
        # phantom visit cards in the UI).
        self.assertEqual(records["by_visit"]["b92d1e8f-accc-48d4-84dd-2eb3e0d9fdca"], [])
        # The merged list is unchanged: each patient-level record appears exactly once.
        self.assertEqual([r.get("id") for r in records["records"]], ["cond-1", "enc-1", "med-1"])
        self.assertEqual(records["errors"], {})

    def test_records_step_resolves_facility_for_folded_encounter(self):
        # The visit with the data is outside the per-visit cap, so its
        # Encounter arrives via the patient-level fetch — the facility
        # reference on it must still be resolved into the visit bucket,
        # which is where the journey UI reads the facility from.
        self.set_aws_settings()
        self.set_settings(
            mpi_id_resolution="/v1/mpi/patient",
            patient_resources=[{"resource_type": "Encounter", "patient_param": "subject"}],
        )
        empty = {"resourceType": "Bundle", "entry": []}
        encounter = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Encounter",
                        "id": "enc-1",
                        # upstream sends episodeOfCare as a 0..N array
                        "episodeOfCare": [{"reference": "EpisodeOfCare/v-2"}],
                        "serviceProvider": {"reference": "Organization/org-1"},
                    }
                }
            ],
        }
        org = {"resourceType": "Organization", "id": "org-1", "name": "TEST REFERRAL HOSPITAL"}
        with patch.object(
            patient.requests,
            "get",
            side_effect=[
                http_response(consent_payload()),  # consent step
                http_response(empty),  # per-visit fetch for the consent visit (empty)
                http_response(self.mpi_payload()),  # MPI resolution
                http_response(encounter),  # Encounter: shr
                http_response(empty),  # Encounter: cr
                http_response(org),  # facility resolved by id after the fold
            ],
        ) as mock_get:
            self.run_flow(["consent", "records"], {"cr_id": "CR1"})
        self.assertTrue(mock_get.call_args_list[5].args[0].endswith("/r4/Organization/org-1"))
        data = frappe.local.response.get("data")
        records = data["results"]["records"]
        self.assertEqual(
            [(r.get("resourceType"), r.get("id")) for r in records["by_visit"]["v-2"]],
            [("Encounter", "enc-1"), ("Organization", "org-1")],
        )
        self.assertEqual(records["errors"], {})


class TestFetchPatientRecords(IntegrationTestCase):
    VISIT_ID = "d7fd81f8-bb9f-4f5d-8a8e-fc12014aa0d6"
    ACCESS_KEY = "AKIAEXAMPLEKEY00000001"
    SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    REGION = "eu-west-2"
    DATA_STORE_ID = "0123456789abcdef0123456789abcdef01"

    def bundle_payload(self):
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": 3,
            "entry": [
                {"resource": {"resourceType": "Encounter", "id": "enc-1", "status": "finished"}},
                {"resource": {"resourceType": "Practitioner", "id": "pract-1"}},
                {"resource": {"resourceType": "Observation", "id": "obs-1", "status": "final"}},
            ],
        }

    def expected_params(self):
        return [
            ("episode-of-care", self.VISIT_ID),
            ("_include", "Encounter:service-provider"),
            ("_revinclude", "Observation:encounter"),
            ("_revinclude", "Condition:encounter"),
        ]

    def expected_query(self):
        from urllib.parse import quote

        encoded = sorted((quote(str(key), safe="-_.~"), quote(str(value), safe="-_.~")) for key, value in self.expected_params())
        return "&".join(key + "=" + value for key, value in encoded)

    def expected_signature(self, amz_date, host, path, canonical_query):
        date = amz_date[:8]
        canonical_request = "\n".join(
            [
                "GET",
                path,
                canonical_query,
                "host:{}\nx-amz-date:{}\n".format(host, amz_date),
                "host;x-amz-date",
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            ]
        )
        scope = "{}/{}/{}/aws4_request".format(date, self.REGION, "healthlake")
        string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
        key = ("AWS4" + self.SECRET_KEY).encode("utf-8")
        for part in (date, self.REGION, "healthlake", "aws4_request"):
            key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
        return hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    def setUp(self):
        doc = frappe.get_doc("HISI Settings")
        for key, value in {
            "data_store_id": self.DATA_STORE_ID,
            "aws_access_key": self.ACCESS_KEY,
            "aws_secret_key": self.SECRET_KEY,
            "aws_region": self.REGION,
        }.items():
            doc.set(key, value)
        doc.set(
            "query_mapper",
            [
                {"param_key": "_include", "param_value": "Encounter:service-provider"},
                {"param_key": "_revinclude", "param_value": "Observation:encounter"},
                {"param_key": "_revinclude", "param_value": "Condition:encounter"},
            ],
        )
        doc.save(ignore_permissions=True)

    def test_fetch_builds_signed_request_from_settings(self):
        class fixed_clock:
            @staticmethod
            def now(tz=None):
                return datetime(2026, 10, 28, 18, 35, 37, tzinfo=timezone.utc)

        host = "healthlake.{}.amazonaws.com".format(self.REGION)
        path = "/datastore/{}/r4/Encounter".format(self.DATA_STORE_ID)
        query = self.expected_query()
        with patch.object(patient.requests, "get", return_value=http_response(self.bundle_payload())) as mock_get, patch.object(
            patient, "datetime", fixed_clock
        ):
            records = patient.fetch_patient_records(self.VISIT_ID)
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(mock_get.call_args.args[0], "https://{}{}?{}".format(host, path, query))
        headers = mock_get.call_args.kwargs["headers"]
        self.assertEqual(headers["X-Amz-Date"], "20261028T183537Z")
        scope = "20261028/{}/healthlake/aws4_request".format(self.REGION)
        self.assertTrue(headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential={}/{}".format(self.ACCESS_KEY, scope)))
        self.assertIn("SignedHeaders=host;x-amz-date", headers["Authorization"])
        self.assertEqual(headers["Authorization"].rsplit("Signature=", 1)[1], self.expected_signature("20261028T183537Z", host, path, query))
        self.assertEqual([r.get("resourceType") for r in records], ["Encounter", "Practitioner", "Observation"])

    def test_fetch_requires_visit_id(self):
        with self.assertRaises(patient.BadRequest):
            patient.fetch_patient_records("")

    def test_fetch_requires_aws_settings(self):
        doc = frappe.get_doc("HISI Settings")
        doc.set("data_store_id", "")
        doc.save(ignore_permissions=True)
        with self.assertRaises(patient.BadRequest) as ctx:
            patient.fetch_patient_records(self.VISIT_ID)
        self.assertIn("data_store_id", str(ctx.exception))

    def test_fetch_parses_single_resource(self):
        with patch.object(patient.requests, "get", return_value=http_response({"resource": {"resourceType": "Encounter", "id": "enc-9"}})):
            records = patient.fetch_patient_records(self.VISIT_ID)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["resourceType"], "Encounter")

    def test_fetch_empty_bundle(self):
        with patch.object(patient.requests, "get", return_value=http_response({"resourceType": "Bundle", "entry": []})):
            self.assertEqual(patient.fetch_patient_records(self.VISIT_ID), [])

    def provider_bundle(self):
        return {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Encounter",
                        "id": "enc-1",
                        "serviceProvider": {"reference": "Organization/org-1"},
                    }
                }
            ],
        }

    def provider_org(self):
        return {
            "resourceType": "Organization",
            "id": "org-1",
            "name": "TEST REFERRAL HOSPITAL",
            "type": [{"coding": [{"code": "HOSPITAL LEVEL 5", "display": "HOSPITAL LEVEL 5"}]}],
        }

    def test_fetch_resolves_service_provider_organization(self):
        # The visit-scoped search returns the Encounter with only a facility
        # reference; the by-id GET reveals the Organization the journey UI
        # renders as the visit's facility.
        with patch.object(
            patient.requests, "get", side_effect=[http_response(self.provider_bundle()), http_response(self.provider_org())]
        ) as mock_get:
            records = patient.fetch_patient_records(self.VISIT_ID)
        self.assertEqual(mock_get.call_count, 2)
        self.assertTrue(mock_get.call_args_list[1].args[0].endswith("/r4/Organization/org-1"))
        self.assertEqual(
            [(r.get("resourceType"), r.get("id")) for r in records],
            [("Encounter", "enc-1"), ("Organization", "org-1")],
        )
        name = next(entry["value"] for entry in records[1].get("headline", []) if entry["label"] == "Name")
        self.assertEqual(name, "TEST REFERRAL HOSPITAL")

    def test_fetch_shares_organization_cache_between_visits(self):
        # A journey resolves the same facility across several visits: the
        # by-id GET happens once, and every visit's bucket still carries the
        # Organization.
        cache = {}
        with patch.object(
            patient.requests,
            "get",
            side_effect=[
                http_response(self.provider_bundle()),
                http_response(self.provider_org()),
                http_response(self.provider_bundle()),
            ],
        ) as mock_get:
            first = patient.fetch_patient_records("v-1", cache)
            second = patient.fetch_patient_records("v-2", cache)
        self.assertEqual(mock_get.call_count, 3)
        for records in (first, second):
            self.assertIn(("Organization", "org-1"), [(r.get("resourceType"), r.get("id")) for r in records])

    def test_fetch_tolerates_unresolvable_service_provider(self):
        # A failed by-id GET drops the facility, not the visit's records.
        with patch.object(
            patient.requests, "get", side_effect=[http_response(self.provider_bundle()), http_response(None, status_code=404)]
        ):
            records = patient.fetch_patient_records(self.VISIT_ID)
        self.assertEqual([(r.get("resourceType"), r.get("id")) for r in records], [("Encounter", "enc-1")])


class TestPatientLevelRecords(IntegrationTestCase):
    SHR_ID = "KE-SHRP-8B626E2B-2B15-4C93-9735"
    REGION = "eu-west-2"
    DATA_STORE_ID = "0123456789abcdef0123456789abcdef01"

    def setUp(self):
        doc = frappe.get_doc("HISI Settings")
        for key, value in {
            "base_url": "https://api.dha.go.ke",
            "consumer_key": "KEY",
            "consumer_secret": "SECRET",
            "username": "USER",
            "password": "PASS",
            "data_store_id": self.DATA_STORE_ID,
            "aws_access_key": "AKIAEXAMPLEKEY00000001",
            "aws_secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "aws_region": self.REGION,
            "mpi_id_resolution": "/v1/mpi/patient",
            "enable_uat_simulation": 0,
        }.items():
            doc.set(key, value)
        doc.set(
            "patient_resources",
            [
                {"resource_type": "Observation", "patient_param": "subject"},
                {"resource_type": "DocumentReference", "patient_param": "patient"},
            ],
        )
        doc.save(ignore_permissions=True)

    def test_resolve_shr_patient_id_parses_mpi_envelope(self):
        payload = {
            "status": "success",
            "message": "Success",
            "status_code": 200,
            "data": {
                "patient": {
                    "status": "success",
                    "message": "MPI record found.",
                    "data": {"cr_id": "CR1", "mpi_id": "KE-MPI-D2AB96DC-8971-409A-8DD4-71431E4E88AB", "shr_id": self.SHR_ID},
                },
                "facility": None,
                "practitioner": None,
            },
            "pagination": {},
        }
        with patch.object(patient.requests, "get", return_value=http_response(payload)) as mock_get:
            shr_id = patient.resolve_shr_patient_id("CR1")
        self.assertEqual(shr_id, self.SHR_ID)
        self.assertEqual(mock_get.call_args.args[0], "https://api.dha.go.ke/v1/mpi/patient")
        self.assertEqual(mock_get.call_args.kwargs["params"], {"cr_id": "CR1"})
        self.assertEqual(
            mock_get.call_args.kwargs["headers"]["Authorization"],
            "Basic " + base64.b64encode(b"USER:PASS").decode("ascii"),
        )

    def test_resolve_shr_patient_id_empty_cr_id(self):
        self.assertEqual(patient.resolve_shr_patient_id(""), "")
        self.assertEqual(patient.resolve_shr_patient_id("   "), "")

    def test_resolve_shr_patient_id_missing_setting(self):
        doc = frappe.get_doc("HISI Settings")
        doc.set("mpi_id_resolution", "")
        doc.save(ignore_permissions=True)
        with self.assertRaises(patient.BadRequest) as ctx:
            patient.resolve_shr_patient_id("CR1")
        self.assertIn("mpi_id_resolution", str(ctx.exception))

    def test_fetch_patient_level_records_signed_request_per_resource(self):
        observations = http_response({"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "Observation", "id": "obs-1"}}]})
        documents = http_response(
            {"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "DocumentReference", "id": "doc-1"}}]}
        )
        with patch.object(patient.requests, "get", side_effect=[observations, documents]) as mock_get:
            by_resource, errors = patient.fetch_patient_level_records(self.SHR_ID)
        self.assertEqual(errors, {})
        host = "healthlake.{}.amazonaws.com".format(self.REGION)
        paths = [call.args[0] for call in mock_get.call_args_list]
        self.assertIn("https://{}/datastore/{}/r4/Observation?subject={}".format(host, self.DATA_STORE_ID, self.SHR_ID), paths)
        self.assertIn("https://{}/datastore/{}/r4/DocumentReference?patient={}".format(host, self.DATA_STORE_ID, self.SHR_ID), paths)
        self.assertEqual([r.get("id") for r in by_resource["Observation"]], ["obs-1"])
        self.assertEqual([r.get("id") for r in by_resource["DocumentReference"]], ["doc-1"])

    def test_fetch_patient_level_records_upstream_error_isolated(self):
        documents = http_response({"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "DocumentReference", "id": "doc-1"}}]})
        with patch.object(patient.requests, "get", side_effect=[http_response(None, status_code=502), documents]):
            by_resource, errors = patient.fetch_patient_level_records(self.SHR_ID)
        self.assertEqual(list(by_resource), ["DocumentReference"])
        self.assertIn("502", errors["Observation"])

    def test_fetch_patient_level_records_searches_both_identifiers(self):
        doc = frappe.get_doc("HISI Settings")
        doc.set("patient_resources", [{"resource_type": "Observation", "patient_param": "subject"}])
        doc.save(ignore_permissions=True)
        by_shr = http_response({"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "Observation", "id": "obs-1"}}]})
        by_cr = http_response({"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "Observation", "id": "obs-2"}}]})
        with patch.object(patient.requests, "get", side_effect=[by_shr, by_cr]) as mock_get:
            by_resource, errors = patient.fetch_patient_level_records(self.SHR_ID, "CR1")
        self.assertEqual(errors, {})
        host = "healthlake.{}.amazonaws.com".format(self.REGION)
        paths = [call.args[0] for call in mock_get.call_args_list]
        self.assertEqual(paths[0], "https://{}/datastore/{}/r4/Observation?subject={}".format(host, self.DATA_STORE_ID, self.SHR_ID))
        self.assertEqual(paths[1], "https://{}/datastore/{}/r4/Observation?subject=CR1".format(host, self.DATA_STORE_ID))
        self.assertEqual([r.get("id") for r in by_resource["Observation"]], ["obs-1", "obs-2"])

    def test_fetch_patient_level_records_multiple_resources_per_row(self):
        doc = frappe.get_doc("HISI Settings")
        doc.set("patient_resources", [{"resource_type": "Condition, ServiceRequest", "patient_param": "subject"}])
        doc.save(ignore_permissions=True)
        by_condition = http_response({"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "Condition", "id": "cond-1"}}]})
        by_service_request = http_response(
            {"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "ServiceRequest", "id": "sr-1"}}]}
        )
        with patch.object(patient.requests, "get", side_effect=[by_condition, by_service_request]) as mock_get:
            by_resource, errors = patient.fetch_patient_level_records(self.SHR_ID)
        self.assertEqual(errors, {})
        host = "healthlake.{}.amazonaws.com".format(self.REGION)
        paths = [call.args[0] for call in mock_get.call_args_list]
        self.assertEqual(paths[0], "https://{}/datastore/{}/r4/Condition?subject={}".format(host, self.DATA_STORE_ID, self.SHR_ID))
        self.assertEqual(paths[1], "https://{}/datastore/{}/r4/ServiceRequest?subject={}".format(host, self.DATA_STORE_ID, self.SHR_ID))
        self.assertEqual([r.get("id") for r in by_resource["Condition"]], ["cond-1"])
        self.assertEqual([r.get("id") for r in by_resource["ServiceRequest"]], ["sr-1"])

    def test_fetch_patient_level_records_defaults_to_subject_param(self):
        doc = frappe.get_doc("HISI Settings")
        doc.set("patient_resources", [{"resource_type": "Observation"}])
        doc.save(ignore_permissions=True)
        with patch.object(patient.requests, "get", return_value=http_response({"resourceType": "Bundle", "entry": []})) as mock_get:
            patient.fetch_patient_level_records(self.SHR_ID)
        host = "healthlake.{}.amazonaws.com".format(self.REGION)
        self.assertEqual(mock_get.call_args.args[0], "https://{}/datastore/{}/r4/Observation?subject={}".format(host, self.DATA_STORE_ID, self.SHR_ID))


class TestVisitClaimContext(IntegrationTestCase):
    """The claim<->visit join: claim bundle_id -> Shared Health Record ->
    the submitted FHIR bundle -> the Encounter's episodeOfCare (the consent
    visit_id space)."""

    BUNDLE = {
        "id": "b-ok",
        "entry": [
            {"resource": {"resourceType": "Claim", "id": "c1"}},
            {"resource": {
                "resourceType": "Encounter",
                "id": "enc-1",
                "episodeOfCare": [{"reference": "EpisodeOfCare/v-1"}],
            }},
        ],
    }

    def test_visit_from_fhir_bundle_episode_of_care_reference(self):
        self.assertEqual(patient.visit_from_fhir_bundle(self.BUNDLE), "v-1")

    def test_visit_from_fhir_bundle_visit_identifier_fallback(self):
        bundle = {"entry": [{
            "resource": {
                "resourceType": "Encounter",
                "identifier": [{"system": "https://x/fhir/identifier/visit", "value": "visit-1"}],
            },
        }]}
        self.assertEqual(patient.visit_from_fhir_bundle(bundle), "visit-1")

    def test_visit_from_fhir_bundle_no_encounter(self):
        bundle = {"entry": [{"resource": {"resourceType": "Claim", "id": "c1"}}]}
        self.assertEqual(patient.visit_from_fhir_bundle(bundle), "")
        self.assertEqual(patient.visit_from_fhir_bundle(None), "")

    def test_parse_shr_fhir_payload_json_python_repr_and_garbage(self):
        self.assertEqual(patient.parse_shr_fhir_payload('{"id": "b1"}'), {"id": "b1"})
        # UAT rows store the payload as a Python dict repr (single quotes)
        self.assertEqual(patient.parse_shr_fhir_payload("{'id': 'b1'}"), {"id": "b1"})
        self.assertEqual(patient.parse_shr_fhir_payload({"id": "b1"}), {"id": "b1"})
        self.assertIsNone(patient.parse_shr_fhir_payload(""))
        self.assertIsNone(patient.parse_shr_fhir_payload(None))
        self.assertIsNone(patient.parse_shr_fhir_payload("not a payload"))

    def test_visit_claim_context_links_claim_via_shr_bundle(self):
        with patch.object(patient, "http_get", return_value={"data": [{"name": "b-ok", "fhir_request": json.dumps(self.BUNDLE)}]}):
            claim_map, by_visit, problems = patient.visit_claim_context([({"name": "c1", "bundle_id": "b-ok"}, "/v1/med/resource/Claim Record")])
        self.assertEqual(claim_map, {"c1": "v-1"})
        self.assertEqual(problems, [])
        self.assertEqual([resource["resourceType"] for resource in by_visit["v-1"]], ["Claim", "Encounter"])

    def test_visit_claim_context_dedupes_bundles_and_collects_problems(self):
        calls = []

        def fake_http_get(url, params, bearer=True, basic=""):
            calls.append((url, params.get("filters")))
            if "b-bad" in params.get("filters", ""):
                raise patient.UpstreamError("SHR lookup failed")
            return {"data": [{"name": "b-ok", "fhir_request": json.dumps(self.BUNDLE)}]}

        with patch.object(patient, "http_get", side_effect=fake_http_get):
            claim_map, by_visit, problems = patient.visit_claim_context(
                [
                    ({"name": "c1", "bundle_id": "b-ok"}, "/v1/med/resource/Claim Record"),
                    ({"name": "c2", "bundle_id": "b-ok"}, "/v1/med/resource/Claim Record"),
                    ({"name": "c3", "bundle_id": "b-bad"}, "/v1/preauth/resource/Claim Record"),
                    ({"name": "c4"}, "/v1/med/resource/Claim Record"),
                ]
            )
        self.assertEqual(claim_map, {"c1": "v-1", "c2": "v-1", "c3": "", "c4": ""})
        self.assertEqual(len(problems), 1)
        self.assertIn("b-bad", problems[0])
        # the shared bundle is fetched once; the preauth claim's bundle is
        # looked up under the preauth route
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], '[["name", "=", "b-ok"]]')
        self.assertIn("/v1/preauth/resource/Shared Health Record", calls[1][0])
        self.assertEqual(calls[1][1], '[["name", "=", "b-bad"]]')

    def test_visit_claim_context_shr_without_payload_leaves_claim_unlinked(self):
        with patch.object(patient, "http_get", return_value={"data": [{"name": "b-empty", "fhir_request": ""}]}):
            claim_map, by_visit, problems = patient.visit_claim_context([({"name": "c1", "bundle_id": "b-empty"}, "/v1/med/resource/Claim Record")])
        self.assertEqual(claim_map, {"c1": ""})
        self.assertEqual(by_visit, {})
        self.assertEqual(problems, [])

    def test_attach_claim_visit_ids_resolves_the_consent_space_visit(self):
        records = [
            {"name": "c1", "bundle_id": "b-ok"},
            {"name": "c2"},
        ]
        with patch.object(patient, "http_get", return_value={"data": [{"name": "b-ok", "fhir_request": json.dumps(self.BUNDLE)}]}):
            patient._attach_claim_visit_ids(records, "/v1/med/resource/Claim Record")
        # The claim is tagged with its bundle's Encounter episodeOfCare —
        # the GUID in the consent visit_id space the visit card shows, so
        # the records join on the same id.
        self.assertEqual(records[0]["visit_id"], "v-1")
        # A claim without a bundle id degrades to "" (the UI falls back to
        # the time-matched consent anchor).
        self.assertEqual(records[1]["visit_id"], "")

    def test_attach_claim_visit_ids_no_records_no_fetch(self):
        with patch.object(patient, "http_get") as mock_get:
            patient._attach_claim_visit_ids([], "/v1/med/resource/Claim Record")
        mock_get.assert_not_called()
