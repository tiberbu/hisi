# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""One-time loader: the SHA benefits matrices → ``HISI Benefit Intervention``.

Run with::

	bench --site hisi-local execute hisi.api.ai.benefits_loader.load_benefits_registry

Idempotent — rows upsert on ``intervention_code``. Sheet selection is
generic: a workbook sheet is parsed when it carries an ``Intervention Code``
column (matrix detail sheets) or a ``System Code`` column (the document-code
register); rules / flow / cover sheets have neither and yield zero rows.
The POMSF matrix (F2) loads after F1 and wins on shared codes; the
SHA-19 document-requirements sheet is joined onto the intervention rows.

The chemo-protocol sheet (regimen-level costs without intervention codes)
is intentionally NOT loaded — it stays on-file reference knowledge.
"""

import re
from pathlib import Path

import frappe
import openpyxl

from hisi.api.ai.benefits_registry import REGISTRY, normalize_intervention_code

_DATA_DIR = Path(__file__).parent
_DEFAULT_F1 = _DATA_DIR / "Benefits Matrix 05.06.25 (version 1) (1).xlsx"
_DEFAULT_F2 = _DATA_DIR / "Reviewed POMSF Benefits Matrix 19.11.25 (2).xlsx"
_DEFAULT_DOCS = _DATA_DIR / "Document_Codes_SHA-19 (1).xlsx"

#: Cell values that mean "no value" in the matrices.
_EMPTY = ("", "n/a", "na", "none", "-", "—")


def _cell(value) -> str:
	return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def _clean(value) -> str:
	text = _cell(value)
	return "" if text.lower() in _EMPTY else text


def _fmt_tariff(value) -> str:
	"""Normalize a tariff cell: pure numbers → comma-grouped.

	Float noise from Excel (``448000.00000000006``) rounds to the integer;
	non-numeric cells (``Upto 150,000``) pass through unchanged."""
	text = _clean(value)
	if not text or not re.fullmatch(r"[\d,\. ]+", text):
		return text
	plain = text.replace(",", "").replace(" ", "")
	try:
		number = float(plain)
	except ValueError:
		return text
	if abs(number - round(number)) < 0.01:
		return f"{int(round(number)):,}"
	return f"{round(number, 2):,.2f}"


def _headers(ws) -> list[tuple[int, str]]:
	first = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
	return [(index, _cell(cell).lower()) for index, cell in enumerate(first)]


def _find(headers: list[tuple[int, str]], *needles: str) -> int | None:
	for index, header in headers:
		if all(needle in header for needle in needles):
			return index
	return None


def parse_matrix_sheet(ws, source: str) -> list[dict]:
	"""Parse one matrix detail sheet (requires an ``Intervention Code`` col).

	Columns are located by header text, so the F1/F2 header variants
	(``Tariffs (KES)`` vs ``First payer SHA (SHIF Tariffs (KES)``) both map.
	Rows without a normalizable code are skipped.
	"""
	headers = _headers(ws)
	code_col = _find(headers, "intervention code")
	if code_col is None:
		return []
	benefit_code_col = _find(headers, "sha code")
	benefit_name_col = _find(headers, "benefit name")
	sub_col = _find(headers, "sub", "benefit")
	name_col = _find(headers, "intervention name")
	facility_col = _find(headers, "category of health facility", "facility")
	complexity_col = _find(headers, "complexity")
	# Several sheets carry two tariff columns (``SHIF Tariffs`` + ``POMF
	# Tariff``); POMSF-only categories leave the SHA side empty, so the
	# first non-empty column wins per row.
	tariff_cols = [index for index, header in headers if "tariff" in header]
	mechanism_col = _find(headers, "payment")
	access_col = _find(headers, "access")
	pomf_col = _find(headers, "pomf limit", "pomsf limit")
	if pomf_col is None:
		for index, header in headers:
			if header.startswith("pomf") and "mechanism" not in header:
				pomf_col = index
				break
	freq_col = _find(headers, "quantity per year")

	def get(row, col: int | None) -> str:
		if col is None or col >= len(row) or row[col] is None:
			return ""
		return _cell(row[col])

	rows: list[dict] = []
	for row in ws.iter_rows(min_row=2, values_only=True):
		code = normalize_intervention_code(row[code_col]) if code_col < len(row) else None
		if not code:
			continue
		tariff = ""
		for col in tariff_cols:
			tariff = _fmt_tariff(get(row, col))
			if tariff:
				break
		rows.append(
			{
				"intervention_code": code,
				"benefit_code": _clean(get(row, benefit_code_col)),
				"intervention_name": _clean(get(row, name_col)) or code,
				"benefit_name": _clean(get(row, benefit_name_col)),
				"sub_benefit": _clean(get(row, sub_col)),
				"complexity": _clean(get(row, complexity_col)),
				"facility_range": _clean(get(row, facility_col)),
				"tariff": tariff,
				"payment_mechanism": _clean(get(row, mechanism_col)),
				"access_path": _clean(get(row, access_col)),
				"pomf_limit": _clean(get(row, pomf_col)),
				"frequency": _clean(get(row, freq_col)),
				"source": source,
			}
		)
	return rows


def parse_doc_register(ws, source: str = "DOC:Document Code Register") -> list[dict]:
	"""Parse the document-code register (``System Code`` column)."""
	headers = _headers(ws)
	code_col = _find(headers, "system code")
	if code_col is None:
		return []
	label_col = _find(headers, "document label")
	basis_col = _find(headers, "code basis")
	mandatory_col = _find(headers, "intervention codes", "mandatory")
	optional_col = _find(headers, "intervention codes", "optional")

	def get(row, col: int | None) -> str:
		if col is None or col >= len(row) or row[col] is None:
			return ""
		return _cell(row[col])

	rows: list[dict] = []
	for row in ws.iter_rows(min_row=2, values_only=True):
		code = _clean(get(row, code_col)).lower()
		if not code:
			continue
		rows.append(
			{
				"intervention_code": code,
				"benefit_code": "DOC",
				"intervention_name": _clean(get(row, label_col)) or code,
				"sub_benefit": _clean(get(row, basis_col)),
				"related_codes": _clean(get(row, mandatory_col)),
				"optional_documents": _clean(get(row, optional_col)),
				"source": source,
			}
		)
	return rows


def parse_sha19_docs(ws) -> dict[str, dict]:
	"""Per-intervention document requirements from the SHA-19 docs sheet."""
	headers = _headers(ws)
	code_col = _find(headers, "intervention code")
	if code_col is None:
		return {}
	name_col = _find(headers, "intervention name")
	sub_col = _find(headers, "sub", "benefit")
	mandatory_col = _find(headers, "mandatory documents")
	investigation_col = _find(headers, "mandatory investigation")
	optional_col = _find(headers, "optional documents")

	def get(row, col: int | None) -> str:
		if col is None or col >= len(row) or row[col] is None:
			return ""
		return _cell(row[col])

	out: dict[str, dict] = {}
	for row in ws.iter_rows(min_row=2, values_only=True):
		code = normalize_intervention_code(row[code_col]) if code_col < len(row) else None
		if not code:
			continue
		out[code] = {
			"intervention_name": _clean(get(row, name_col)),
			"sub_benefit": _clean(get(row, sub_col)),
			"mandatory_documents": _clean(get(row, mandatory_col)),
			"mandatory_investigations": _clean(get(row, investigation_col)),
			"optional_documents": _clean(get(row, optional_col)),
		}
	return out


def _load_workbook(path: Path):
	return openpyxl.load_workbook(str(path), read_only=True, data_only=True)


def load_benefits_registry(f1_path=None, f2_path=None, doc_path=None) -> dict:
	"""Load all three files into the registry; returns a run summary."""
	f1 = Path(f1_path or _DEFAULT_F1)
	f2 = Path(f2_path or _DEFAULT_F2)
	docs = Path(doc_path or _DEFAULT_DOCS)

	rows: dict[str, dict] = {}
	order: list[str] = []
	sheet_counts: dict[str, int] = {}

	def add_matrix(parsed: list[dict]):
		for row in parsed:
			code = row["intervention_code"]
			if code not in rows:
				order.append(code)
			rows[code] = row  # F2 (loaded later) wins on shared codes

	wb1 = _load_workbook(f1)
	for ws in wb1.worksheets:
		parsed = parse_matrix_sheet(ws, f"F1:{ws.title.strip()}")
		if parsed:
			sheet_counts[f"F1:{ws.title.strip()}"] = len(parsed)
			add_matrix(parsed)
	wb1.close()

	wb2 = _load_workbook(f2)
	for ws in wb2.worksheets:
		parsed = parse_matrix_sheet(ws, f"F2:{ws.title.strip()}")
		if parsed:
			sheet_counts[f"F2:{ws.title.strip()}"] = len(parsed)
			add_matrix(parsed)
	wb2.close()

	wbd = _load_workbook(docs)
	for ws in wbd.worksheets:
		title = ws.title.strip().lower()
		if "code register" in title:
			parsed = parse_doc_register(ws)
			if parsed:
				sheet_counts["DOC:Document Code Register"] = len(parsed)
				add_matrix(parsed)
		elif "sha-19 docs" in title:
			join = parse_sha19_docs(ws)
			sheet_counts["DOC:SHIF-POMSF SHA-19 Docs"] = len(join)
			for code, doc in join.items():
				base = rows.get(code)
				if base is None:
					base = {
						"intervention_code": code,
						"benefit_code": "SHA-19",
						"intervention_name": doc.get("intervention_name") or code,
						"benefit_name": "Surgical Services",
						"source": "DOC:SHIF-POMSF SHA-19 Docs",
					}
					order.append(code)
					rows[code] = base
				if doc.get("intervention_name"):
					base["intervention_name"] = doc["intervention_name"]
				if doc.get("sub_benefit"):
					base["sub_benefit"] = doc["sub_benefit"]
				for field in ("mandatory_documents", "mandatory_investigations", "optional_documents"):
					if doc.get(field):
						base[field] = doc[field]
	wbd.close()

	existing = {
		row["intervention_code"]: row["name"]
		for row in frappe.get_all(REGISTRY, fields=["name", "intervention_code"])
	}
	created = updated = 0
	for code in order:
		row = dict(rows[code])
		name = existing.get(code)
		if name:
			frappe.db.set_value(
				REGISTRY, name, {key: value for key, value in row.items() if key != "intervention_code"}
			)
			updated += 1
		else:
			frappe.get_doc({"doctype": REGISTRY, **row}).insert()
			created += 1
	if (created or updated) and not frappe.flags.in_test:
		frappe.db.commit()
	return {
		"created": created,
		"updated": updated,
		"total": len(order),
		"sheets": sheet_counts,
	}
