# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""Unit tests for the deterministic rule engine (the v2 Phase A/B core).

Pure-function tests: the evaluators, the evidence verifier, the redaction
layer, and the engine's normalization — no LLM, no network, no DB (the
context is built in memory).
"""

import unittest

from hisi.api.ai.rules import evaluators as ev
from hisi.api.ai.rules import redact
from hisi.api.ai.rules.context import RuleContext
from hisi.api.ai.rules.engine import run_rules
from hisi.api.ai.rules.evidence import EvidenceRef, verify_ref


def _claim(name="CLM-1", **kw):
	base = {
		"name": name,
		"client": "CR-1",
		"client_name": "John Doe",
		"facility": "FAC-1",
		"facility_name": "Test Hospital",
		"creation": "2026-05-02 10:00:00",
		"creation_date": "2026-05-02",
		"claim_status": "Submitted",
		"claim_amount": 50000.0,
		"claim_subtype": "",
		"use": "claim",
		"date_start": "2026-05-01",
		"date_end": "2026-05-01",
		"interventions_code": ["SHA-12-001"],
		"interventions": ["Lab tests"],
		"diagnoses_code": ["E11.9"],
		"diagnoses": ["Diabetes"],
		"visit_id": "V-1",
		"bundle_id": "B-1",
	}
	base.update(kw)
	return base


def _consent(name="CNS-1", visit_id="V-1", facility_id="FAC-1", status="Approved"):
	return {
		"name": name,
		"client": "CR-1",
		"visit_id": visit_id,
		"facility_id": facility_id,
		"consent_status": status,
		"status": status,
		"creation": "2026-04-28 09:00:00",
		"creation_date": "2026-04-28",
	}


def _ctx(claims=None, preauths=None, consents=None, eligibility=None, contributions=None, shr_by_visit=None, registry=None, errors=None):
	return RuleContext(
		cr_id="CR-1",
		sources=["consent", "eligibility", "contributions", "claims", "preauth"],
		errors=errors or {},
		eligibility=eligibility if eligibility is not None else {"eligible": True, "status": "active", "scheme": "SHA", "schemes": []},
		consents=consents if consents is not None else [_consent()],
		consent_by_visit={c["visit_id"]: c for c in (consents if consents is not None else [_consent()])},
		claims=claims if claims is not None else [_claim()],
		preauths=preauths or [],
		contributions=contributions,
		shr_by_visit=shr_by_visit or {},
		registry=registry or {},
	)


def _tags(*codes):
	return [{"tag_code": c, "label": c, "severity": "medium", "rule_type": "code", "rule_key": c.lower(), "parameters": "", "severity_policy": "fixed", "version": 1} for c in codes]


class TestOrphanClaim(unittest.TestCase):
	def test_claim_without_consent_is_flagged(self):
		out = ev.eval_orphan_claim(_ctx(consents=[], shr_by_visit={}), {})
		self.assertEqual(len(out), 1)
		self.assertEqual(out[0]["tag_code"], "ORPHAN_CLAIM")

	def test_consent_visit_passes(self):
		out = ev.eval_orphan_claim(_ctx(), {})
		self.assertEqual(out, [])

	def test_unresolvable_visit_is_flagged(self):
		out = ev.eval_orphan_claim(_ctx(claims=[_claim(visit_id="unknown-visitId-2026-05-01")]), {})
		self.assertEqual(len(out), 1)

	def test_consent_source_error_skips(self):
		out = ev.eval_orphan_claim(_ctx(consents=[], errors={"consent": "boom"}), {})
		self.assertEqual(out, [])

	def test_unapproved_consent_is_flagged(self):
		out = ev.eval_orphan_claim(_ctx(consents=[_consent(status="Pending")]), {})
		self.assertEqual(len(out), 1)


class TestUncoveredService(unittest.TestCase):
	def test_ineligible_member_with_dated_claim(self):
		out = ev.eval_uncovered_service(_ctx(eligibility={"eligible": False, "status": "inactive", "scheme": "", "schemes": []}), {})
		self.assertEqual(len(out), 1)

	def test_eligible_member_passes(self):
		out = ev.eval_uncovered_service(_ctx(), {})
		self.assertEqual(out, [])

	def test_source_error_skips(self):
		out = ev.eval_uncovered_service(_ctx(eligibility=None, errors={"eligibility": "boom"}), {})
		self.assertEqual(out, [])


class TestAmountMismatch(unittest.TestCase):
	def test_claim_over_tariff(self):
		ctx = _ctx(claims=[_claim(claim_amount=20000.0, interventions_code=["SHA-16-1"])])
		ctx.registry = {"SHA-16-1": {"intervention_code": "SHA-16-1", "tariff": "10,650", "payment_mechanism": "FFS", "intervention_name": "Hemodialysis"}}
		out = ev.eval_amount_mismatch(ctx, {"tolerance_pct": 5, "tolerance_abs": 100})
		self.assertEqual(len(out), 1)
		self.assertEqual(out[0]["tag_code"], "AMOUNT_MISMATCH")

	def test_within_tolerance_passes(self):
		ctx = _ctx(claims=[_claim(claim_amount=10700.0, interventions_code=["SHA-16-1"])])
		ctx.registry = {"SHA-16-1": {"intervention_code": "SHA-16-1", "tariff": "10,650", "payment_mechanism": "FFS", "intervention_name": "Hemodialysis"}}
		out = ev.eval_amount_mismatch(ctx, {"tolerance_pct": 5, "tolerance_abs": 100})
		self.assertEqual(out, [])

	def test_per_diem_tariff_skipped(self):
		ctx = _ctx(claims=[_claim(claim_amount=99999.0, interventions_code=["SHA-07-1"])])
		ctx.registry = {"SHA-07-1": {"intervention_code": "SHA-07-1", "tariff": "2,240", "payment_mechanism": "Per Diem", "intervention_name": "Inpatient"}}
		out = ev.eval_amount_mismatch(ctx, {})
		self.assertEqual(out, [])

	def test_claim_vs_preauth_deviation(self):
		ctx = _ctx(
			claims=[_claim(name="CLM-2", claim_amount=50000.0)],
			preauths=[_claim(name="PRE-2", claim_amount=20000.0)],
		)
		out = ev.eval_amount_mismatch(ctx, {})
		self.assertEqual(len(out), 1)


class TestMissingPreauth(unittest.TestCase):
	def test_surgical_code_without_preauth(self):
		out = ev.eval_missing_preauth(_ctx(claims=[_claim(interventions_code=["SHA-19-001"])]), {})
		self.assertEqual(len(out), 1)

	def test_surgical_code_with_preauth(self):
		out = ev.eval_missing_preauth(
			_ctx(claims=[_claim(interventions_code=["SHA-19-001"])], preauths=[_claim(name="PRE-1", interventions_code=["SHA-19-001"])]),
			{},
		)
		self.assertEqual(out, [])

	def test_non_surgical_code_without_registry_passes(self):
		out = ev.eval_missing_preauth(_ctx(claims=[_claim(interventions_code=["SHA-12-001"])]), {})
		self.assertEqual(out, [])


class TestDuplicateClaim(unittest.TestCase):
	def test_two_identical_claims(self):
		out = ev.eval_duplicate_claim(_ctx(claims=[_claim(name="CLM-1"), _claim(name="CLM-2")]), {})
		self.assertEqual(len(out), 1)

	def test_different_dates_pass(self):
		out = ev.eval_duplicate_claim(
			_ctx(claims=[_claim(name="CLM-1"), _claim(name="CLM-2", date_start="2026-06-01", date_end="2026-06-01")]),
			{},
		)
		self.assertEqual(out, [])


class TestContributionGap(unittest.TestCase):
	def test_gap_detected(self):
		contributions = {
			"cr_id": "CR-1",
			"contributions": [
				{"payment_reference": "P1", "period": "2026-01"},
				{"payment_reference": "P2", "period": "2026-09"},
			],
		}
		out = ev.eval_contribution_gap(_ctx(contributions=contributions), {"max_gap_months": 3})
		self.assertGreaterEqual(len(out), 1)

	def test_consecutive_payments_pass(self):
		contributions = {
			"cr_id": "CR-1",
			"contributions": [
				{"payment_reference": "P1", "period": "2026-01"},
				{"payment_reference": "P2", "period": "2026-02"},
				{"payment_reference": "P3", "period": "2026-03"},
			],
		}
		out = ev.eval_contribution_gap(_ctx(contributions=contributions), {"max_gap_months": 12})
		# No internal gap; the tail to "today" may fire depending on the run
		# date — the internal gap must not.
		internal = [f for f in out if "between payment P1" in f["detail"] and "P2" in f["detail"]]
		self.assertEqual(internal, [])


class TestTimingAnomaly(unittest.TestCase):
	def test_service_after_submission(self):
		out = ev.eval_timing_anomaly(_ctx(claims=[_claim(date_start="2026-05-10", date_end="2026-05-10")]), {})
		self.assertEqual(len(out), 1)

	def test_logical_order_passes(self):
		out = ev.eval_timing_anomaly(_ctx(), {})
		self.assertEqual(out, [])


class TestEligibilityMismatch(unittest.TestCase):
	def test_inactive_member_with_claims(self):
		out = ev.eval_eligibility_mismatch(_ctx(eligibility={"eligible": False, "status": "terminated", "scheme": "", "schemes": []}), {})
		self.assertEqual(len(out), 1)

	def test_eligible_passes(self):
		out = ev.eval_eligibility_mismatch(_ctx(), {})
		self.assertEqual(out, [])


class TestTriangulation(unittest.TestCase):
	def test_diagnosis_mismatch(self):
		shr = {"V-1": [{"resourceType": "Condition", "code": {"coding": [{"code": "J06.9", "display": "Acute upper respiratory infection"}]}}]}
		out = ev.eval_tri_diagnosis(_ctx(shr_by_visit=shr), {})
		self.assertEqual(len(out), 1)

	def test_diagnosis_match_passes(self):
		shr = {"V-1": [{"resourceType": "Condition", "code": {"coding": [{"code": "E11.9", "display": "Diabetes"}]}}]}
		out = ev.eval_tri_diagnosis(_ctx(shr_by_visit=shr), {})
		self.assertEqual(out, [])

	def test_surgery_without_procedure(self):
		shr = {"V-1": [{"resourceType": "Encounter", "period": {"start": "2026-05-01", "end": "2026-05-01"}}]}
		out = ev.eval_tri_intervention(_ctx(claims=[_claim(interventions_code=["SHA-19-001"])], shr_by_visit=shr), {})
		self.assertEqual(len(out), 1)

	def test_surgery_with_procedure_passes(self):
		shr = {"V-1": [{"resourceType": "Procedure", "code": {"coding": [{"code": "35611"}]}}]}
		out = ev.eval_tri_intervention(_ctx(claims=[_claim(interventions_code=["SHA-19-001"])], shr_by_visit=shr), {})
		self.assertEqual(out, [])

	def test_claim_start_before_encounter(self):
		shr = {"V-1": [{"resourceType": "Encounter", "period": {"start": "2026-05-05", "end": "2026-05-05"}}]}
		out = ev.eval_tri_dates(_ctx(claims=[_claim(date_start="2026-05-01", date_end="2026-05-01")], shr_by_visit=shr), {})
		self.assertEqual(len(out), 1)

	def test_facility_mismatch(self):
		out = ev.eval_tri_facility(_ctx(consents=[_consent(facility_id="FAC-9")], claims=[_claim(facility="FAC-1")]), {})
		self.assertEqual(len(out), 1)

	def test_facility_match_passes(self):
		out = ev.eval_tri_facility(_ctx(), {})
		self.assertEqual(out, [])


class TestEvidenceVerification(unittest.TestCase):
	def test_verified_ref(self):
		ctx = _ctx()
		ref = EvidenceRef(step="claims", record_ref="CLM-1", field="claim_amount", quoted_value="50,000.00")
		self.assertEqual(verify_ref(ref, ctx), "verified")

	def test_unverified_ref(self):
		ctx = _ctx()
		ref = EvidenceRef(step="claims", record_ref="CLM-1", field="claim_amount", quoted_value="99,999.99")
		self.assertEqual(verify_ref(ref, ctx), "unverified")

	def test_missing_source(self):
		ctx = _ctx()
		ref = EvidenceRef(step="claims", record_ref="CLM-MISSING", field="name", quoted_value="x")
		self.assertEqual(verify_ref(ref, ctx), "missing_source")

	def test_engine_quarantines_unverifiable(self):
		# A tag whose evaluator returns a finding with a broken ref is
		# quarantined by the engine (policy exclude) and recorded in provenance.
		ctx = _ctx(claims=[_claim(name="CLM-1")])

		def broken_evaluator(context, params):
			return [
				{
					"tag_code": "X",
					"severity": "high",
					"title": "broken",
					"detail": "d",
					"evidence": ["claims:CLM-MISSING.name = x"],
					"_refs": [EvidenceRef(step="claims", record_ref="CLM-MISSING", field="name", quoted_value="x")],
				}
			]

		from hisi.api.ai.rules import engine as eng

		original = eng.EVALUATORS["orphan_claim"]
		eng.EVALUATORS["orphan_claim"] = broken_evaluator
		try:
			out = run_rules(ctx, _tags("ORPHAN_CLAIM"), {})
		finally:
			eng.EVALUATORS["orphan_claim"] = original
		self.assertEqual(out["result"]["findings"], [])
		self.assertEqual(out["quarantined"], 1)
		self.assertEqual(out["provenance"][0]["verified"], False)


class TestRedaction(unittest.TestCase):
	def test_member_pii_masked(self):
		context = {
			"cr_id": "CR2024123456AB",
			"sources": ["patient", "claims"],
			"data": {
				"patient": {
					"name": "John Doe",
					"first_name": "John",
					"last_name": "Doe",
					"cr_id": "CR2024123456AB",
					"msisdn": "+254712345678",
					"email": "john@example.com",
					"date_of_birth": "1990-04-12",
				},
				"claims": {"records": [{"client_name": "John Doe", "client": "CR2024123456AB"}]},
			},
			"errors": {},
		}
		out = redact.redact_context_for_llm(context)
		self.assertIn("•", out["cr_id"])
		self.assertNotEqual(out["cr_id"], "CR2024123456AB")
		self.assertEqual(out["data"]["patient"]["first_name"], "•••")
		self.assertIn("•••", out["data"]["patient"]["msisdn"])
		self.assertIn("•••@", out["data"]["patient"]["email"])
		self.assertEqual(out["data"]["patient"]["date_of_birth"], "1990")
		self.assertEqual(out["data"]["claims"]["records"][0]["client_name"], "•••")

	def test_policy_off(self):
		context = {"cr_id": "CR1234", "sources": [], "data": {"patient": {"name": "John", "msisdn": "+254712345678"}}, "errors": {}}
		out = redact.redact_context_for_llm(context, {"cr_id": False, "names": False, "phone": False, "email": False, "id_numbers": False, "dob": False})
		self.assertEqual(out["cr_id"], "CR1234")
		self.assertEqual(out["data"]["patient"]["msisdn"], "+254712345678")


class TestRunRulesEndToEnd(unittest.TestCase):
	def test_severity_honors_tag_on_dict_snapshot(self):
		# Snapshot rows are JSON dicts — the default "fixed" policy must still
		# honor the tag's severity (regression: getattr on dicts dropped it).
		ctx = _ctx(consents=[], shr_by_visit={})
		tags = [
			{
				"tag_code": "ORPHAN_CLAIM",
				"label": "Orphan claim",
				"severity": "high",
				"rule_type": "code",
				"rule_key": "orphan_claim",
				"parameters": "",
				"severity_policy": "fixed",
				"version": 1,
			}
		]
		out = run_rules(ctx, tags, {})
		self.assertTrue(out["result"]["findings"])
		for finding in out["result"]["findings"]:
			self.assertEqual(finding["severity"], "high")
		self.assertEqual(out["result"]["overall"], "major")

	def test_policy_table_overrides(self):
		ctx = _ctx(consents=[], shr_by_visit={})
		tags = [
			{
				"tag_code": "ORPHAN_CLAIM",
				"label": "Orphan claim",
				"severity": "high",
				"rule_type": "code",
				"rule_key": "orphan_claim",
				"parameters": "",
				"severity_policy": "policy_table",
				"version": 1,
			}
		]
		out = run_rules(ctx, tags, {"severity_overrides": '{"ORPHAN_CLAIM": "medium"}'})
		self.assertEqual(out["result"]["findings"][0]["severity"], "medium")
	def test_contract_shape_and_overall(self):
		ctx = _ctx(
			claims=[
				_claim(name="CLM-1", interventions_code=["SHA-19-001"]),
				_claim(name="CLM-2"),
				_claim(name="CLM-3"),
			],
			consents=[],
			eligibility={"eligible": False, "status": "terminated", "scheme": "", "schemes": []},
			shr_by_visit={},
		)
		out = run_rules(ctx, _tags("ORPHAN_CLAIM", "ELIGIBILITY_MISMATCH", "DUPLICATE_CLAIM", "MISSING_PREAUTH"), {})
		result = out["result"]
		self.assertIn(result["overall"], ("clean", "minor", "major"))
		self.assertEqual(result["overall"], "major")
		self.assertTrue(result["findings"])
		for finding in result["findings"]:
			self.assertIn("tag_code", finding)
			self.assertIn("severity", finding)
			self.assertNotIn("_refs", finding)  # internal keys stay out of the contract
		# Provenance: one entry per finding, all verified.
		self.assertEqual(len(out["provenance"]), len(result["findings"]))
		self.assertTrue(all(p["verified"] for p in out["provenance"]))
		self.assertTrue(result["summary"])
		self.assertTrue(result["executive_summary"])


if __name__ == "main":
	unittest.main()
