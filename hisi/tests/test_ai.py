# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from hisi.api.ai import worker
from hisi.api.ai.prompt import (
	OUTPUT_CONTRACT,
	build_data_context,
	build_report_prompt,
	extract_json_object,
	normalize_findings,
	parse_llm_json,
	render_report_markdown,
)
from hisi.api.ai.seed import DEFAULT_TAGS, ensure_default_tags
from hisi.api.ai.settings import get_ai_data_sources, get_ai_settings
from hisi.api.response_handler import BadRequest

AI_SINGLE = "HISI AI Settings"
REPORT = "HISI Discrepancy Report"
TAG = "HISI Discrepancy Tag"


def _clear_ai_single() -> None:
	"""Remove any HISI AI Settings row so the defaults apply. Deliberately
	NO commit: the per-class test rollback owns the transaction, so a commit
	here would leak the clear (and clobber the operator's live single) past
	the class boundary. This fork stores singles in the ``Singles`` table
	(no per-single tables)."""
	frappe.db.delete("Singles", {"doctype": AI_SINGLE})


def _empty_sources_patches():
	"""Patch the worker's data-source seam to empty (no upstream traffic)."""
	return (
		patch.object(worker, "step_consent", return_value={"records": []}),
		patch.object(worker, "fetch_patient_contributions", return_value={"cr_id": "C", "total": 0, "contributions": []}),
		patch.object(worker, "step_claims", return_value={"records": []}),
		patch.object(worker, "step_preauth", return_value={"records": []}),
	)


def _make_report(cr_id: str = "CRTEST") -> object:
	doc = frappe.get_doc(
		{
			"doctype": REPORT,
			"naming_series": "HIS-DRP-.YYYY.-.####",
			"subject_cr_id": cr_id,
			"status": "Queued",
			"tag_snapshot": json.dumps(
				[{"tag_code": "DUPLICATE_CLAIM", "label": "Duplicate claim", "instruction": "same service twice", "severity": "high"}]
			),
		}
	)
	doc.insert()
	return doc


def _terminal(doc_name: str) -> dict:
	# cache=False: the worker's guarded UPDATEs bypass the get_value cache
	# (the fork keeps a per-key value cache; the vidval worker tests read
	# terminal state the same way).
	row = frappe.db.get_value(REPORT, doc_name, ["status", "error", "report", "findings"], as_dict=True, cache=False)
	return row or {}


class TestPromptAssembly(IntegrationTestCase):
	def test_prompt_pins_output_contract_and_sections(self):
		context = {
			"cr_id": "CR1",
			"sources": ["claims"],
			"data": {"claims": {"records": [{"name": "C1"}]}},
			"errors": {},
		}
		tags = [
			{"tag_code": "DUPLICATE_CLAIM", "label": "Duplicate claim", "instruction": "same service twice", "severity": "high"},
			{"tag_code": "TIMING_ANOMALY", "label": "Timing anomaly", "instruction": "", "severity": "low"},
		]
		prompt = build_report_prompt(None, context, tags)
		# the contract is appended verbatim, byte-for-byte
		self.assertTrue(prompt.endswith(OUTPUT_CONTRACT))
		self.assertIn("- DUPLICATE_CLAIM: Duplicate claim — same service twice (severity: high)", prompt)
		self.assertIn("- TIMING_ANOMALY: Timing anomaly (severity: low)", prompt)
		self.assertIn('"cr_id": "CR1"', prompt)
		# an empty system prompt falls back to the documented default
		self.assertTrue(prompt.startswith("You are a health-insurance claims auditor."))

	def test_prompt_uses_configured_system_prompt(self):
		prompt = build_report_prompt("Custom auditor prompt.", {"cr_id": "X"}, [])
		self.assertTrue(prompt.startswith("Custom auditor prompt."))
		self.assertIn("- (none declared)", prompt)


class TestParse(IntegrationTestCase):
	def test_extract_json_object_fenced(self):
		raw = 'Here is the report:\n```json\n{"summary": "s", "findings": []}\n```\nDone.'
		obj = extract_json_object(raw)
		self.assertEqual(obj["summary"], "s")

	def test_extract_json_object_prose_with_braces_in_string(self):
		# the first balanced span that parses as a JSON object wins; braces
		# inside JSON strings do not affect the depth count
		raw = 'noise {stray braces} tail {"summary": "ok", "findings": []}'
		obj = extract_json_object(raw)
		self.assertEqual(obj["summary"], "ok")

	def test_extract_json_object_string_braces(self):
		# a valid object whose value contains braces is parsed correctly
		obj = extract_json_object('{"a": "b{c}"}')
		self.assertEqual(obj["a"], "b{c}")

	def test_extract_json_object_invalid(self):
		self.assertIsNone(extract_json_object("no json here"))
		self.assertIsNone(extract_json_object(""))

	def test_parse_llm_json_requires_findings_list(self):
		self.assertIsNone(parse_llm_json('{"summary": "x"}'))
		parsed = parse_llm_json('{"summary": "x", "overall": "MAJOR", "findings": [{"tag_code": "A"}]}')
		self.assertEqual(parsed["overall"], "major")

	def test_normalize_findings_coerces_and_dedupes(self):
		parsed = {
			"summary": "s",
			"overall": "clean",  # must be recomputed from the findings
			"findings": [
				{"tag_code": "A", "severity": "WEIRD", "title": "T", "detail": "D", "evidence": "one string"},
				{"tag_code": "A", "severity": "WEIRD", "title": "T", "detail": "D", "evidence": ["e1", "  ", "e2"]},  # dup of #1
				{"tag_code": "B", "severity": "high"},
			],
		}
		result = normalize_findings(parsed)
		self.assertEqual(len(result["findings"]), 2)  # the duplicate A collapses
		self.assertEqual(result["overall"], "major")  # the high finding wins
		self.assertEqual(result["findings"][0]["severity"], "medium")  # unknown severity coerced
		self.assertEqual(result["findings"][0]["evidence"], ["one string"])  # string evidence coerced
		self.assertEqual(result["findings"][1]["evidence"], [])
		self.assertEqual(result["findings"][1]["title"], "B")  # title falls back to the code

	def test_normalize_findings_clean_when_empty(self):
		result = normalize_findings({"summary": "", "overall": "major", "findings": []})
		self.assertEqual(result["overall"], "clean")

	def test_render_report_markdown_clean(self):
		md = render_report_markdown("CR1", "Jane Doe", {"summary": "all good", "overall": "clean", "findings": []}, model="qwen3-27b")
		self.assertIn("# Discrepancy Report — CR1", md)
		self.assertIn("Member: Jane Doe", md)
		self.assertIn("Overall: **clean** · Findings: 0", md)
		self.assertIn("No discrepancies found", md)
		self.assertIn("Model: qwen3-27b", md)

	def test_render_report_markdown_with_findings(self):
		md = render_report_markdown(
			"CR2",
			"",
			{
				"summary": "bad",
				"overall": "major",
				"findings": [
					{"tag_code": "A", "severity": "high", "title": "Dup", "detail": "twice", "evidence": ["claim C1", "claim C2"]},
				],
			},
		)
		self.assertIn("### 1. [high] Dup (A)", md)
		self.assertIn("**Evidence**", md)
		self.assertIn("- claim C1", md)


class TestDataContext(IntegrationTestCase):
	def test_build_data_context_caps_records(self):
		results = {
			"claims": {"records": [{"i": i} for i in range(50)]},
			"contributions": {"cr_id": "C", "total": 30, "contributions": [{"i": i} for i in range(40)]},
			"records": {"by_visit": {"v2": [{"i": 1}, {"i": 2}], "v1": [{"i": 3}]}, "errors": {}},
		}
		context = build_data_context("C", ["claims", "contributions", "records"], results, {"consent": "boom"}, 5)
		self.assertEqual(len(context["data"]["claims"]["records"]), 5)
		self.assertEqual(len(context["data"]["contributions"]["contributions"]), 5)
		# by_visit merged in deterministic (sorted visit) order
		self.assertEqual(context["data"]["records"]["records"], [{"i": 3}, {"i": 1}, {"i": 2}])
		self.assertEqual(context["errors"], {"consent": "boom"})
		self.assertEqual(context["sources"], ["claims", "contributions", "records"])


class TestSettings(IntegrationTestCase):
	def tearDown(self):
		_clear_ai_single()
		super().tearDown()

	def test_settings_defaults_without_single(self):
		_clear_ai_single()
		s = get_ai_settings()
		self.assertIsNone(s.llm_base_url)
		self.assertEqual(s.llm_model, "qwen3-27b")
		self.assertEqual(s.llm_idle_timeout_sec, 120)
		self.assertEqual(s.llm_total_timeout_sec, 600)
		self.assertEqual(s.discrepancy_max_records, 100)

	def test_data_sources_validated_against_known_steps(self):
		_clear_ai_single()
		self.assertEqual(get_ai_data_sources(), ["consent", "contributions", "claims", "preauth"])
		frappe.get_doc({"doctype": AI_SINGLE, "discrepancy_data_sources": "claims, bogus, consent"}).save()
		self.assertEqual(get_ai_data_sources(), ["claims", "consent"])

	def test_data_sources_fall_back_on_garbage(self):
		frappe.get_doc({"doctype": AI_SINGLE, "discrepancy_data_sources": "nonsense,also-bad"}).save()
		self.assertEqual(get_ai_data_sources(), ["consent", "contributions", "claims", "preauth"])


class TestSeed(IntegrationTestCase):
	def test_seed_is_idempotent_and_preserves_edits(self):
		known = {t[0] for t in DEFAULT_TAGS}
		existing = {row.tag_code for row in frappe.get_all(TAG, fields=["tag_code"])}
		created = ensure_default_tags()
		self.assertEqual(created, len(known - existing))
		all_codes = {row.tag_code for row in frappe.get_all(TAG, fields=["tag_code"])}
		self.assertTrue(known <= all_codes)
		# second call: no-op
		self.assertEqual(ensure_default_tags(), 0)
		# an operator edit survives re-seeding
		doc = frappe.get_doc(TAG, "DUPLICATE_CLAIM")
		doc.db_set("label", "Renamed duplicate")
		ensure_default_tags()
		self.assertEqual(frappe.db.get_value(TAG, "DUPLICATE_CLAIM", "label"), "Renamed duplicate")


class TestWorkerFailClosed(IntegrationTestCase):
	def tearDown(self):
		_clear_ai_single()
		super().tearDown()

	def test_unconfigured_llm_fails_closed(self):
		_clear_ai_single()
		doc = _make_report()
		p1, p2, p3, p4 = _empty_sources_patches()
		with p1, p2, p3, p4:
			worker.run_discrepancy_report(doc.name)
		row = _terminal(doc.name)
		self.assertEqual(row["status"], "Failed")
		self.assertEqual(json.loads(row["error"])["code"], "LLM_NOT_CONFIGURED")

	def test_all_sources_erroring_fails_data_unavailable(self):
		doc = _make_report()
		bad = BadRequest("upstream down")
		p1 = patch.object(worker, "step_consent", side_effect=bad)
		p2 = patch.object(worker, "fetch_patient_contributions", side_effect=bad)
		p3 = patch.object(worker, "step_claims", side_effect=bad)
		p4 = patch.object(worker, "step_preauth", side_effect=bad)
		with p1, p2, p3, p4:
			worker.run_discrepancy_report(doc.name)
		row = _terminal(doc.name)
		self.assertEqual(row["status"], "Failed")
		self.assertEqual(json.loads(row["error"])["code"], "DATA_UNAVAILABLE")

	def test_transport_retry_exactly_once_then_fail(self):
		frappe.get_doc({"doctype": AI_SINGLE, "llm_base_url": "http://127.0.0.1:9/v1"}).save()
		doc = _make_report()
		calls = {"n": 0}

		def boom(**kwargs):
			calls["n"] += 1
			raise worker.llm_client.LLMConnectionError("no route to host")

		p1, p2, p3, p4 = _empty_sources_patches()
		p5 = patch.object(worker.llm_client, "stream_chat_completion", side_effect=boom)
		with p1, p2, p3, p4, p5:
			worker.run_discrepancy_report(doc.name)
		self.assertEqual(calls["n"], 2)  # initial + exactly one transport retry
		row = _terminal(doc.name)
		self.assertEqual(row["status"], "Failed")
		self.assertEqual(json.loads(row["error"])["code"], "LLM_HTTP_ERROR")

	def test_parse_failure_one_corrective_retry_then_success(self):
		frappe.get_doc({"doctype": AI_SINGLE, "llm_base_url": "http://127.0.0.1:9/v1"}).save()
		doc = _make_report()
		replies = iter(
			[
				"Sorry, here you go: {not json",
				json.dumps({"summary": "ok", "overall": "clean", "findings": []}),
			]
		)
		calls = {"n": 0}

		def fake_stream(**kwargs):
			calls["n"] += 1
			return next(replies), {"total_tokens": 5}

		p1, p2, p3, p4 = _empty_sources_patches()
		p5 = patch.object(worker.llm_client, "stream_chat_completion", side_effect=fake_stream)
		with p1, p2, p3, p4, p5:
			worker.run_discrepancy_report(doc.name)
		self.assertEqual(calls["n"], 2)  # initial + exactly one corrective retry
		row = _terminal(doc.name)
		self.assertEqual(row["status"], "Success")
		findings = json.loads(row["findings"])
		self.assertEqual(findings["overall"], "clean")
		self.assertIn("Discrepancy Report", row["report"])
