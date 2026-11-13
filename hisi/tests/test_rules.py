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
from hisi.api.ai.rules.evidence import EvidenceRef, verify_findings, verify_ref


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
	def test_flagged_code_without_preauth(self):
		ctx = _ctx(claims=[_claim(interventions_code=["SHA-19-001"])])
		ctx.registry = {"SHA-19-001": {"intervention_code": "SHA-19-001", "intervention_name": "Surgery", "preauth_required": 1}}
		out = ev.eval_missing_preauth(ctx, {})
		self.assertEqual(len(out), 1)

	def test_flagged_code_with_preauth(self):
		ctx = _ctx(
			claims=[_claim(interventions_code=["SHA-19-001"])],
			preauths=[_claim(name="PRE-1", interventions_code=["SHA-19-001"])],
		)
		ctx.registry = {"SHA-19-001": {"intervention_code": "SHA-19-001", "intervention_name": "Surgery", "preauth_required": 1}}
		out = ev.eval_missing_preauth(ctx, {})
		self.assertEqual(out, [])

	def test_unflagged_code_passes_even_if_sha19(self):
		# Not every intervention requires preauth: an explicit flag of 0 means
		# the check stays silent even for SHA-19 family codes.
		ctx = _ctx(claims=[_claim(interventions_code=["SHA-19-002"])])
		ctx.registry = {"SHA-19-002": {"intervention_code": "SHA-19-002", "intervention_name": "Surgery", "preauth_required": 0}}
		out = ev.eval_missing_preauth(ctx, {})
		self.assertEqual(out, [])

	def test_unknown_flag_passes(self):
		# No registry/live profile for the code → requirement unknown → silent.
		out = ev.eval_missing_preauth(_ctx(claims=[_claim(interventions_code=["SHA-12-001"])]), {})
		self.assertEqual(out, [])

	def test_live_yes_string_flag(self):
		ctx = _ctx(claims=[_claim(interventions_code=["SHA-16-007"])])
		ctx.registry = {"SHA-16-007": {"intervention_code": "SHA-16-007", "intervention_name": "Hemodialysis", "preauth_required": True}}
		out = ev.eval_missing_preauth(ctx, {})
		self.assertEqual(len(out), 1)


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


class TestContributionIsContextOnly(unittest.TestCase):
	def test_contribution_gap_no_longer_a_rule(self):
		# Contributions are context only (the payer makes contribution
		# decisions): there is no deterministic evaluator that scores them.
		self.assertFalse(hasattr(ev, "eval_contribution_gap"))

	def test_timing_anomaly_ignores_contributions(self):
		# A future-period contribution entry must not surface as a finding.
		contributions = {
			"cr_id": "CR-1",
			"contributions": [
				{"payment_reference": "P1", "period": "2099-01"},
				{"payment_reference": "P2", "period": "2099-02"},
			],
		}
		out = ev.eval_timing_anomaly(_ctx(contributions=contributions), {})
		self.assertEqual(out, [])

	def test_coverage_counts_contribution_lane(self):
		contributions = {
			"contributions": [
				{"payment_reference": "P1", "period": "2026-01", "amount": 100},
				{"payment_reference": "P2", "period": "2026-02", "amount": 200},
			]
		}
		ctx = _ctx(contributions=contributions)
		cov = ctx.coverage("Member full history")
		by_key = {lane["key"]: lane for lane in cov["lanes"]}
		self.assertEqual(by_key["contributions"]["status"], "available")
		self.assertEqual(by_key["contributions"]["count"], 2)
		self.assertEqual(cov["scope"], "Member full history")

	def test_contribution_ledger_sorted_and_grouped(self):
		contributions = {
			"contributions": [
				{"payment_reference": "P2", "period": "2026-02", "amount": 200},
				{"payment_reference": "P1", "period": "2026-01", "amount": 100},
			]
		}
		ctx = _ctx(contributions=contributions)
		ledger = ctx.contribution_ledger()
		self.assertEqual([e["payment_reference"] for e in ledger], ["P1", "P2"])

	def test_coverage_error_detail_is_neutral(self):
		# The raw lane error is shown in the report: internal doctype/config
		# names must not leak into the deliverable.
		ctx = _ctx(errors={"consent": "HISI Settings fs_base_url / fs_auth are not configured."})
		cov = ctx.coverage("")
		by_key = {lane["key"]: lane for lane in cov["lanes"]}
		detail = by_key["consent"]["detail"]
		self.assertNotIn("HISI", detail)
		self.assertTrue(detail.startswith("site settings"))


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


class TestClaimVsShrData(unittest.TestCase):
	"""The claims-sent vs SHR-data-available comparison, both directions.

	The directionality checks only run when the records lane was fetched and
	did not error (an empty SHR there would mean unknown, not absent)."""

	@staticmethod
	def _ctx_with_records(claims=None, consents=None, preauths=None, shr_by_visit=None, visits=None, records_failed=False, records_fetched=True):
		claims = claims if claims is not None else [_claim()]
		consents = consents if consents is not None else [_consent()]
		consent_by_visit = {c["visit_id"]: c for c in consents}
		shr_by_visit = shr_by_visit or {}
		if visits is None:
			visits = [
				{"visit_id": key, "consented": key in consent_by_visit, "shr_resources": records}
				for key, records in shr_by_visit.items()
			]
		return RuleContext(
			cr_id="CR-1",
			sources=(
				["consent", "eligibility", "contributions", "claims", "preauth", "records"]
				if records_fetched
				else ["consent", "eligibility", "contributions", "claims", "preauth"]
			),
			errors={"records": "boom"} if records_failed else {},
			eligibility={"eligible": True, "status": "active", "scheme": "SHA", "schemes": []},
			consents=consents,
			consent_by_visit=consent_by_visit,
			claims=claims,
			preauths=preauths or [],
			visits=visits,
			shr_by_visit=shr_by_visit,
			records={} if records_fetched and not records_failed else None,
			registry={},
		)

	def test_claim_with_empty_shr_is_flagged(self):
		out = ev.eval_claim_without_shr_data(self._ctx_with_records(shr_by_visit={}), {})
		self.assertEqual(len(out), 1)
		self.assertEqual(out[0]["tag_code"], "CLAIM_WITHOUT_SHR_DATA")

	def test_claim_with_shr_data_passes(self):
		shr = {"V-1": [{"resourceType": "Encounter", "id": "enc-1"}]}
		out = ev.eval_claim_without_shr_data(self._ctx_with_records(shr_by_visit=shr), {})
		self.assertEqual(out, [])

	def test_records_lane_failure_skips(self):
		out = ev.eval_claim_without_shr_data(self._ctx_with_records(records_failed=True), {})
		self.assertEqual(out, [])

	def test_records_lane_not_fetched_skips(self):
		out = ev.eval_claim_without_shr_data(self._ctx_with_records(records_fetched=False), {})
		self.assertEqual(out, [])

	def test_unresolvable_visit_skipped(self):
		out = ev.eval_claim_without_shr_data(
			self._ctx_with_records(claims=[_claim(visit_id="unknown-visitId-2026-05-01")]), {}
		)
		self.assertEqual(out, [])

	def test_preauth_without_shr_not_flagged(self):
		# The rule is about claims that assert rendered service; a preauth
		# precedes the service, so an empty SHR is expected, not a finding.
		out = ev.eval_claim_without_shr_data(self._ctx_with_records(claims=[]), {})
		self.assertEqual(out, [])

	def test_shr_data_without_claim_flagged(self):
		shr = {"V-1": [{"resourceType": "Encounter", "id": "enc-1"}, {"resourceType": "Observation", "id": "obs-1"}]}
		out = ev.eval_shr_data_without_claim(self._ctx_with_records(claims=[], shr_by_visit=shr), {})
		self.assertEqual(len(out), 1)
		self.assertEqual(out[0]["tag_code"], "SHR_DATA_WITHOUT_CLAIM")

	def test_shr_data_with_claim_passes(self):
		shr = {"V-1": [{"resourceType": "Encounter", "id": "enc-1"}]}
		out = ev.eval_shr_data_without_claim(self._ctx_with_records(shr_by_visit=shr), {})
		self.assertEqual(out, [])

	def test_shr_data_referenced_by_preauth_passes(self):
		shr = {"V-1": [{"resourceType": "Encounter", "id": "enc-1"}]}
		out = ev.eval_shr_data_without_claim(
			self._ctx_with_records(claims=[], preauths=[_claim(name="PA-1", use="preauth")], shr_by_visit=shr), {}
		)
		self.assertEqual(out, [])

	def test_unconsented_visit_not_flagged(self):
		shr = {"V-9": [{"resourceType": "Encounter", "id": "enc-1"}]}
		out = ev.eval_shr_data_without_claim(
			self._ctx_with_records(claims=[], consents=[], shr_by_visit=shr), {}
		)
		self.assertEqual(out, [])

	def test_out_of_scope_visit_not_flagged(self):
		# A scoped report must not flag a visit outside its scope: the
		# scope's visit list (ctx.visits) carries only V-1, so V-2 — which
		# has SHR data and no referencing claim — is invisible to this run.
		# V-1 is in scope but already referenced by the claim, so it is
		# skipped too. Net: no findings.
		shr = {"V-1": [{"resourceType": "Encounter", "id": "enc-1"}], "V-2": [{"resourceType": "Observation", "id": "obs-1"}]}
		out = ev.eval_shr_data_without_claim(
			self._ctx_with_records(
				claims=[_claim(visit_id="V-1")],
				shr_by_visit=shr,
				visits=[{"visit_id": "V-1", "consented": True, "shr_resources": [{"resourceType": "Encounter", "id": "enc-1"}]}],
			),
			{},
		)
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


class TestBenefitsExchangeNormalizer(unittest.TestCase):
	"""The live BMX payload → registry-row normalizer (tolerant key matching,
	authoritative preauth flag)."""

	def _norm(self, payload):
		from hisi.api.ai.benefits_exchange import normalize_live_profile

		return normalize_live_profile("SHA-12-001", payload)

	def test_flat_payload_with_cap_and_preauth_true(self):
		row = self._norm(
			{
				"intervention_code": "SHA-12-001",
				"intervention_name": "Laboratory tests",
				"sub_benefit": "Outpatient",
				"cap": 500,
				"payment_mechanism": "FFS",
				"preauth_required": True,
			}
		)
		self.assertEqual(row["tariff"], 500)
		self.assertIs(row["preauth_required"], True)

	def test_wrapped_data_list(self):
		row = self._norm({"success": True, "data": [{"intervention_name": "CT scan", "amount": 6900, "authorization_required": "no"}]})
		self.assertEqual(row["intervention_name"], "CT scan")
		self.assertEqual(row["tariff"], 6900)
		self.assertIs(row["preauth_required"], False)

	def test_preauth_string_variants(self):
		from hisi.api.ai.benefits_exchange import normalize_live_profile

		for value, expected in (("Yes", True), ("1", True), ("Yes, prior authorization", None), ("N/A", False)):
			row = normalize_live_profile("X-1-1", {"intervention_name": "n", "preauth_required": value})
			self.assertIs(row["preauth_required"], expected, f"{value!r} -> {row['preauth_required']}")

	def test_preauth_via_authorization_type(self):
		row = self._norm({"intervention_name": "Cath", "authorization_type": "Preauthorization"})
		self.assertIs(row["preauth_required"], True)

	def test_empty_payload_is_a_miss(self):
		self.assertIsNone(self._norm({"data": []}))
		self.assertIsNone(self._norm("not a dict"))

	def test_real_dha_bmx_wire_format(self):
		# Authoritative sample shape from the live UAT BMX endpoint
		# (2026-11-13): data list wrapper, "tarrif"/"tarrif_limit" spelling,
		# needs_preauth flag, service_category, benefits_exchange_code.
		row = self._norm(
			{
				"data": [
					{
						"name": "INT-00920",
						"intervention_name": "Consultation",
						"intervention_code": "SHA-12-001",
						"benefits_exchange_code": "TBX-10021",
						"service_category": "Outpatient",
						"payment_mechanism": "Capitation",
						"tarrif": 75.0,
						"tarrif_limit": 75.0,
						"needs_preauth": 0,
						"retired": 0,
					}
				]
			}
		)
		self.assertEqual(row["intervention_name"], "Consultation")
		self.assertEqual(row["tariff"], 75.0)
		self.assertIs(row["preauth_required"], False)
		self.assertEqual(row["sub_benefit"], "Outpatient")
		self.assertEqual(row["benefit_code"], "TBX-10021")
		self.assertEqual(row["payment_mechanism"], "Capitation")


if __name__ == "main":
	unittest.main()


class TestClaimNotDispatched(unittest.TestCase):
	"""Claims whose status (per HISI Claim Status config) means they were NOT
	sent to the payer are surfaced as context — so payment-oriented findings on
	them read as provisional. Unknown dispatch state (unconfigured status) is
	skipped: the report never assumes a state it cannot verify."""

	def _nd(self, **kw):
		c = _claim(claim_status="failed_dispatch")
		c.update(kw)
		return c

	def test_not_dispatched_claim_is_flagged(self):
		out = ev.eval_claim_not_dispatched(_ctx(claims=[self._nd(sent_to_payer=False, status_note="Not yet in play.")]), {})
		self.assertEqual(len(out), 1)
		f = out[0]
		self.assertEqual(f["tag_code"], "CLAIM_NOT_DISPATCHED")
		self.assertEqual(f["severity"], "medium")
		self.assertIn("failed_dispatch", f["detail"])
		self.assertIn("claim_status", f["evidence"][0])

	def test_dispatched_claim_is_skipped(self):
		out = ev.eval_claim_not_dispatched(_ctx(claims=[self._nd(sent_to_payer=True)]), {})
		self.assertEqual(out, [])

	def test_unknown_dispatch_state_is_skipped(self):
		out = ev.eval_claim_not_dispatched(_ctx(claims=[self._nd(sent_to_payer=None)]), {})
		self.assertEqual(out, [])

	def test_multiple_not_dispatched_claims(self):
		claims = [self._nd(name="CLM-A", sent_to_payer=False), self._nd(name="CLM-B", sent_to_payer=False), _claim(name="CLM-C", sent_to_payer=True)]
		out = ev.eval_claim_not_dispatched(_ctx(claims=claims), {})
		self.assertEqual(len(out), 2)

	def test_evidence_verifies(self):
		c = self._nd(sent_to_payer=False)
		out = ev.eval_claim_not_dispatched(_ctx(claims=[c]), {})
		verified, unverified = verify_findings([out[0]], _ctx(claims=[c]))
		self.assertEqual(unverified, [])
		self.assertEqual(len(verified), 1)


class TestScopeToClaim(unittest.TestCase):
	def _ctx(self):
		a = _claim(name="CLM-1", visit_id="V-1", bundle_id="B-1", interventions_code=["SHA-12-001"])
		b = _claim(name="CLM-2", visit_id="V-2", bundle_id="B-2", interventions_code=["SHA-16-007"])
		pre1 = _claim(name="PR-1", use="preauth", visit_id="V-1", interventions_code=["SHA-12-001"])
		pre2 = _claim(name="PR-2", use="preauth", visit_id="V-2", interventions_code=["SHA-16-007"])
		pre3 = _claim(name="PR-3", use="preauth", visit_id="V-3", interventions_code=["SHA-07-011"])
		return _ctx(claims=[a, b], preauths=[pre1, pre2, pre3])

	def test_scope_by_name(self):
		ctx, target = self._ctx().scope_to_claim("CLM-1")
		self.assertIsNotNone(target)
		self.assertEqual([c["name"] for c in ctx.claims], ["CLM-1"])
		# related preauths: same visit (PR-1) — not the others
		self.assertEqual([p["name"] for p in ctx.preauths], ["PR-1"])
		# member-level context is preserved
		self.assertEqual(ctx.member, None)
		self.assertTrue(ctx.eligibility["eligible"])
		self.assertEqual(len(ctx.consents), 1)

	def test_scope_by_bundle_id(self):
		ctx, target = self._ctx().scope_to_claim("B-2")
		self.assertIsNotNone(target)
		self.assertEqual([c["name"] for c in ctx.claims], ["CLM-2"])
		self.assertEqual([p["name"] for p in ctx.preauths], ["PR-2"])

	def test_scope_preauth_by_overlapping_code(self):
		ctx = _ctx(
			claims=[_claim(name="CLM-9", visit_id="V-9", interventions_code=["SHA-12-001"])],
			preauths=[_claim(name="PR-9", use="preauth", visit_id="V-other", interventions_code=["SHA-12-001"])],
		)
		scoped, target = ctx.scope_to_claim("CLM-9")
		self.assertIsNotNone(target)
		self.assertEqual([p["name"] for p in scoped.preauths], ["PR-9"])

	def test_unknown_claim_returns_self_and_none(self):
		ctx = self._ctx()
		scoped, target = ctx.scope_to_claim("CLM-NOPE")
		self.assertIs(scoped, ctx)
		self.assertIsNone(target)

	def test_empty_ref_returns_self_and_none(self):
		ctx = self._ctx()
		scoped, target = ctx.scope_to_claim("  ")
		self.assertIs(scoped, ctx)
		self.assertIsNone(target)


class TestScopeToVisit(unittest.TestCase):
	def _ctx(self):
		a = _claim(name="CLM-1", visit_id="V-1", bundle_id="B-1", interventions_code=["SHA-12-001"])
		b = _claim(name="CLM-2", visit_id="V-2", bundle_id="B-2", interventions_code=["SHA-16-007"])
		pre1 = _claim(name="PR-1", use="preauth", visit_id="V-1", interventions_code=["SHA-12-001"])
		pre2 = _claim(name="PR-2", use="preauth", visit_id="V-2", interventions_code=["SHA-16-007"])
		return _ctx(claims=[a, b], preauths=[pre1, pre2])

	def test_scope_to_visit(self):
		ctx, target = self._ctx().scope_to_visit("V-1")
		self.assertIsNotNone(target)
		self.assertEqual([c["name"] for c in ctx.claims], ["CLM-1"])
		self.assertEqual([p["name"] for p in ctx.preauths], ["PR-1"])
		# member-level context preserved
		self.assertTrue(ctx.eligibility["eligible"])
		self.assertEqual(len(ctx.consents), 1)

	def test_scope_to_visit_no_records(self):
		ctx, target = self._ctx().scope_to_visit("V-404")
		self.assertIsNone(target)

	def test_scope_to_visit_empty_ref(self):
		base = self._ctx()
		scoped, target = base.scope_to_visit("  ")
		self.assertIs(scoped, base)
		self.assertIsNone(target)

	def test_scope_to_visit_by_registry_visit_request_id(self):
		# The UI references visits by the registry visit_request_id (12-hex)
		# while claims carry the consent visit_id UUID — scoping must bridge
		# both id spaces, and include the same-day claims whose bundles
		# carried no readable encounter (the timeline ties them by date).
		c1 = _consent(name="VCR-20261112-FA507C99", visit_id="eb84922f-9fe6-49fe-9b67-b81bebcfaee5", status="Verified")
		c1.update({"visit_request_id": "6C33D4C9B1BB", "creation": "2026-11-12 08:00:00", "creation_date": "2026-11-12"})
		c2 = _consent(name="VCR-20261030-1738BDC0", visit_id="51634308-ddb2-4f89-8f2b-c6b97384c0d6", status="Verified")
		c2.update({"visit_request_id": "5B4140EAF2DB", "creation": "2026-10-30 08:00:00", "creation_date": "2026-10-30"})
		a = _claim(name="CLM-1", visit_id="eb84922f-9fe6-49fe-9b67-b81bebcfaee5", creation="2026-11-12 09:00:00")
		b = _claim(name="CLM-2", visit_id="51634308-ddb2-4f89-8f2b-c6b97384c0d6", creation="2026-10-30 09:00:00")
		c = _claim(name="CLM-3", visit_id="", creation="2026-11-12 10:00:00", date_start="2026-11-12", date_end="2026-11-12")
		base = _ctx(claims=[a, b, c], consents=[c1, c2])
		scoped, target = base.scope_to_visit("6C33D4C9B1BB")
		self.assertIsNotNone(target)
		self.assertEqual(sorted(x["name"] for x in scoped.claims), ["CLM-1", "CLM-3"])
		self.assertEqual([x["name"] for x in scoped.consents], ["VCR-20261112-FA507C99"])
		# the UUID and the consent name spell the same visit
		scoped2, _ = base.scope_to_visit("eb84922f-9fe6-49fe-9b67-b81bebcfaee5")
		self.assertEqual(sorted(x["name"] for x in scoped2.claims), ["CLM-1", "CLM-3"])
		scoped3, _ = base.scope_to_visit("VCR-20261112-FA507C99")
		self.assertEqual(sorted(x["name"] for x in scoped3.claims), ["CLM-1", "CLM-3"])


class TestScopeToPreauth(unittest.TestCase):
	def _ctx(self):
		c1 = _claim(name="CLM-1", visit_id="V-1", bundle_id="B-1", interventions_code=["SHA-12-001"])
		c2 = _claim(name="CLM-2", visit_id="V-9", bundle_id="B-9", interventions_code=["SHA-99"])
		p1 = _claim(name="PR-1", use="preauth", visit_id="V-1", bundle_id="B-1", interventions_code=["SHA-12-001"])
		return _ctx(claims=[c1, c2], preauths=[p1])

	def test_scope_to_preauth_by_name(self):
		scoped, target = self._ctx().scope_to_preauth("PR-1")
		self.assertIsNotNone(target)
		self.assertEqual([p["name"] for p in scoped.preauths], ["PR-1"])
		# the related claim (same visit, bundle and codes) is kept; CLM-2 drops
		self.assertEqual([c["name"] for c in scoped.claims], ["CLM-1"])

	def test_scope_to_preauth_by_bundle(self):
		scoped, target = self._ctx().scope_to_preauth("B-1")
		self.assertIsNotNone(target)
		self.assertEqual([p["name"] for p in scoped.preauths], ["PR-1"])

	def test_unknown_preauth_returns_self_and_none(self):
		ctx = self._ctx()
		scoped, target = ctx.scope_to_preauth("PR-NOPE")
		self.assertIs(scoped, ctx)
		self.assertIsNone(target)


class TestScopeResults(unittest.TestCase):
	def test_filters_raw_results_to_scoped_context(self):
		from hisi.api.ai import worker as ai_worker

		c1 = _claim(name="CLM-1", visit_id="V-1", bundle_id="B-1")
		c2 = _claim(name="CLM-2", visit_id="V-2", bundle_id="B-2")
		p1 = _claim(name="PR-1", use="preauth", visit_id="V-1")
		results = {
			"patient": {"cr_id": "CR-1", "name": "X"},
			"claims": {"records": [c1, c2]},
			"preauth": {"records": [p1]},
		}
		ctx = _ctx(claims=[c1], preauths=[p1])
		out = ai_worker._scope_results(results, ctx)
		self.assertEqual([r["name"] for r in out["claims"]["records"]], ["CLM-1"])
		self.assertEqual([r["name"] for r in out["preauth"]["records"]], ["PR-1"])
		self.assertEqual(out["patient"], results["patient"])
		# input not mutated
		self.assertEqual(len(results["claims"]["records"]), 2)
