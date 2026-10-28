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
            upstream_api_timeout=60,
            jwt_ttl_s=60,
            uat_base_url="https://uat.dha.go.ke",
            enable_uat_simulation=1,
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
        self.assertEqual(patient_call.args[0], "https://api.dha.go.ke/client-registry/fetch-client")
        patient_params = patient_call.kwargs["params"]
        self.assertEqual(
            json.loads(patient_params["payload"]),
            {"identification_type": "CR ID", "identification_number": "CR2473157541615-8"},
        )
        self.assertIn("Authorization", patient_call.kwargs["headers"])
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(mock_post.call_args.args[0], "https://api.dha.go.ke/backend/api/v5/coverage/validate")
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
        self.assertEqual(data["results"]["consent"]["base_url"], "https://uat.dha.go.ke")
        self.assertTrue(data["simulation"])

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
        self.assertEqual(mock_post.call_args.args[0], "https://api.dha.go.ke/backend/api/v5/coverage/validate")
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
        self.set_settings(uat_base_url="")
        self.run_flow(["consent"], {"cr_id": "CR1"})
        response = frappe.local.response
        self.assertEqual(response.get("code"), "BAD_REQUEST")
        self.assertIn("uat_base_url", response.get("message"))

    def test_consent_requires_fetch_patient_consent(self):
        self.set_settings(fetch_patient_consent="")
        self.run_flow(["consent"], {"cr_id": "CR1"})
        response = frappe.local.response
        self.assertEqual(response.get("code"), "BAD_REQUEST")
        self.assertIn("fetch_patient_consent", response.get("message"))

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

    def test_no_simulation_flag_reflected(self):
        self.set_settings(enable_uat_simulation=0)
        with patch.object(patient.requests, "get", return_value=http_response(patient_payload())):
            self.run_flow(["patient"], {"identification_type": "CR ID", "identification_number": "CR1"})
        self.assertFalse(frappe.local.response.get("data")["simulation"])

    def test_simulation_prefers_uat_base_url_setting(self):
        self.set_settings(uat_base_url="https://uat2.dha.go.ke")
        with patch.object(patient.requests, "get", return_value=http_response(consent_payload())):
            self.run_flow(["consent"], {"cr_id": "CR1"})
        self.assertEqual(frappe.local.response.get("data")["results"]["consent"]["base_url"], "https://uat2.dha.go.ke")

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

    def test_records_step_requires_visit_id(self):
        self.run_flow(["records"], {})
        response = frappe.local.response
        self.assertEqual(response.get("code"), "BAD_REQUEST")
        self.assertIn("visit_id", response.get("message"))


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
