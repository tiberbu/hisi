# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""Tests for the code-driven benefits registry: code normalization/extraction,
prompt injection of matched rows, and the xlsx sheet parsers (hermetic — a
tiny in-memory workbook, no real xlsx and no LLM)."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from hisi.api.ai import benefits_loader
from hisi.api.ai.benefits_registry import (
	extract_benefit_codes,
	matched_benefits_lines,
	normalize_intervention_code,
)
from hisi.api.ai.prompt import OUTPUT_CONTRACT, build_report_prompt

REGISTRY = "HISI Benefit Intervention"


class TestBenefitCodes(IntegrationTestCase):
	def test_normalize_intervention_code(self):
		self.assertEqual(normalize_intervention_code("SHA  -19 - 001"), "SHA-19-001")
		self.assertEqual(normalize_intervention_code("sha-9-002"), "SHA-09-002")
		self.assertEqual(normalize_intervention_code("PMF-12-001"), "PMF-12-001")
		self.assertEqual(normalize_intervention_code("  SHA-16-7  "), "SHA-16-007")

	def test_normalize_rejects_non_codes(self):
		self.assertIsNone(normalize_intervention_code("N/A"))
		self.assertIsNone(normalize_intervention_code("SHA-19"))
		self.assertIsNone(normalize_intervention_code("Aortic Valvuloplasty"))
		self.assertIsNone(normalize_intervention_code(""))

	def test_extract_benefit_codes(self):
		text = json.dumps(
			{
				"line_items": [{"code": "SHA-19-001"}, {"code": "SHA  - 09 - 002"}],
				"docs": ["us-abdomen", "preauthorization-form"],
			}
		)
		codes, docs = extract_benefit_codes(text)
		self.assertIn("SHA-19-001", codes)
		self.assertIn("SHA-09-002", codes)
		self.assertIn("us-abdomen", docs)
		self.assertIn("preauthorization-form", docs)

	def test_extract_finds_nothing_in_plain_text(self):
		codes, docs = extract_benefit_codes("no codes here at all")
		self.assertEqual(codes, [])
		self.assertEqual(docs, [])


class TestBenefitsDetail(IntegrationTestCase):
	def _seed(self, code: str, **kwargs) -> None:
		row = {"doctype": REGISTRY, "intervention_code": code, "intervention_name": "Test Intervention"}
		row.update(kwargs)
		frappe.get_doc(row).insert(ignore_if_duplicate=True)

	def test_prompt_injects_matched_detail(self):
		self._seed("SHA-19-001", benefit_code="SHA-19", tariff="448,000", mandatory_documents="Preauthorization form")
		self._seed("us-abdomen", benefit_code="DOC", intervention_name="Abdominal Ultrasound", related_codes="SHA-19-119")
		context = {"claims": [{"code": "SHA-19-001", "doc": "us-abdomen"}]}
		detail = matched_benefits_lines(json.dumps(context))
		self.assertIn("SHA-19-001", detail)
		self.assertIn("us-abdomen", detail)
		prompt = build_report_prompt(None, context, [], benefits_detail=detail)
		self.assertIn("Benefits Detail (registry rows for codes present in the data):", prompt)
		self.assertIn("448,000 KES", prompt)
		self.assertTrue(prompt.endswith(OUTPUT_CONTRACT))

	def test_no_detail_section_when_empty(self):
		prompt = build_report_prompt(None, {"x": 1}, [])
		self.assertNotIn("Benefits Detail", prompt)
		self.assertTrue(prompt.endswith(OUTPUT_CONTRACT))

	def test_matched_lines_empty_for_unregistered_codes(self):
		self.assertEqual(matched_benefits_lines("only unregistered: SHA-99-999 and zz-zz-zz here"), "")

	def test_matched_lines_empty_without_codes(self):
		self.assertEqual(matched_benefits_lines("plain text, no codes"), "")


class TestLoaderParsers(IntegrationTestCase):
	"""The xlsx parsers are pure functions — drive them with an in-memory workbook."""

	def _sheet(self, headers, rows):
		import openpyxl

		wb = openpyxl.Workbook()
		ws = wb.active
		ws.append(headers)
		for row in rows:
			ws.append(row)
		return ws

	def test_parse_matrix_sheet(self):
		ws = self._sheet(
			[
				"Benefit Name",
				"SHA Code",
				"Sub-Benefit",
				"Intervention Name",
				"Intervention Code",
				"Category of Health Facility",
				"Tariffs",
				"Provider Payment Mechanism",
				"Access Path",
			],
			[
				[
					"Surgical Services",
					"SHA-19",
					"Cardiology",
					"Aortic Valvuloplasty",
					"SHA  -19 - 001",
					"Level 4-6",
					448000.00000000006,
					"Fixed Fee rate",
					"Inpatient",
				],
			],
		)
		rows = benefits_loader.parse_matrix_sheet(ws, "F1:Surgical Services")
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row["intervention_code"], "SHA-19-001")
		self.assertEqual(row["benefit_code"], "SHA-19")
		self.assertEqual(row["tariff"], "448,000")
		self.assertEqual(row["sub_benefit"], "Cardiology")
		self.assertEqual(row["source"], "F1:Surgical Services")

	def test_parse_matrix_sheet_skips_codeless_rows(self):
		ws = self._sheet(
			["Intervention Name", "Intervention Code", "Tariffs"],
			[["No code row", "", "100"], ["N/A row", "N/A", "200"]],
		)
		self.assertEqual(benefits_loader.parse_matrix_sheet(ws, "F1:X"), [])

	def test_parse_doc_register(self):
		ws = self._sheet(
			[
				"Document Label",
				"Document Code",
				"System Code",
				"Code Basis",
				"Shares Code With",
				"Intervention Codes (Mandatory)",
				"Intervention Codes (Optional)",
			],
			[["Abdominal Ultrasound", "Abdo US", "us-abdomen", "Imaging - standard code", "", "SHA-19-119, SHA-19-128", ""]],
		)
		rows = benefits_loader.parse_doc_register(ws)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row["intervention_code"], "us-abdomen")
		self.assertEqual(row["benefit_code"], "DOC")
		self.assertEqual(row["intervention_name"], "Abdominal Ultrasound")
		self.assertEqual(row["related_codes"], "SHA-19-119, SHA-19-128")

	def test_parse_sha19_docs(self):
		ws = self._sheet(
			[
				"#",
				"SHA Code",
				"Sub-Benefit",
				"Intervention Name",
				"Intervention Code",
				"Mandatory documents",
				"Mandatory Investigation  Required(Scanned images of the investigation and reports)",
				"Optional documents",
			],
			[[1, "SHA-19", "Cardiology", "Aortic Valvuloplasty", "SHA  -19 - 001", "Preauthorization form,Clinical Notes", "Electrocardiogram and Echocardiogram", ""]],
		)
		out = benefits_loader.parse_sha19_docs(ws)
		self.assertEqual(len(out), 1)
		row = out["SHA-19-001"]
		self.assertEqual(row["mandatory_documents"], "Preauthorization form,Clinical Notes")
		self.assertEqual(row["mandatory_investigations"], "Electrocardiogram and Echocardiogram")
