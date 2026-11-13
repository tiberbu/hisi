# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""The normalized rule context — the single, deterministic input to the
rule engine.

``RuleContext.build`` takes the worker's raw fetch results (the
``patient_flow`` step outputs plus the claim<->visit join) and normalizes
them: claims/preauths with parsed dates/amounts/code lists, consents keyed
by visit, the eligibility result, the contribution ledger, the per-visit
SHR resources, and the static benefits registry rows for the intervention
codes actually present. Evaluators receive ONLY this object — no DB, no
network, no LLM — so a run is a pure function of the fetched data plus the
tag configuration.
"""

import re
from dataclasses import dataclass, field

import frappe

#: The static benefits registry (seeded from the SHA matrices). Live
#: BenefitsExchange results (Phase D) override these per code when
#: ``benefits_mode = live``.
REGISTRY = "HISI Benefit Intervention"

_CODE_SPLIT_RE = re.compile(r"[,;/|]+")
_CODE_TRIM_RE = re.compile(r"^(SHA|PMF)\s*-\s*(\d{1,2})\s*-\s*(\d{1,3})$", re.IGNORECASE)


def _code_list(value) -> list:
	"""Split a raw interventions/diagnoses code field into a clean list."""
	if value in (None, ""):
		return []
	if isinstance(value, list):
		raw = ",".join(str(v) for v in value)
	else:
		raw = str(value)
	out = []
	for part in _CODE_SPLIT_RE.split(raw):
		code = part.strip()
		if not code:
			continue
		match = _CODE_TRIM_RE.match(code)
		if match:
			family, group, seq = match.groups()
			code = f"{family.upper()}-{int(group):02d}-{int(seq):03d}"
		if code not in out:
			out.append(code)
	return out


def _label_list(value) -> list:
	"""Split a raw interventions/labels field into clean label strings."""
	if value in (None, ""):
		return []
	if isinstance(value, list):
		raw = ",".join(str(v) for v in value)
	else:
		raw = str(value)
	out = []
	for part in _CODE_SPLIT_RE.split(raw):
		label = part.strip()
		if label and label not in out:
			out.append(label)
	return out


def _date_only(value) -> str:
	"""``YYYY-MM-DD`` for any date/datetime string, else ``""``."""
	text = str(value or "").strip()
	if len(text) < 10:
		return ""
	return text[:10]


def _amount(value):
	"""float amount from a raw value; ``None`` when not numeric."""
	if value in (None, ""):
		return None
	try:
		return float(str(value).replace(",", "").strip())
	except (ValueError, TypeError):
		return None


def _normalize_claim(record: dict) -> dict:
	"""One normalized claim/preauthorization record (flat, evaluator-ready)."""
	out = {
		"name": str(record.get("name") or "").strip(),
		"client": str(record.get("client") or "").strip(),
		"client_name": str(record.get("client_name") or "").strip(),
		"facility": str(record.get("facility") or "").strip(),
		"facility_name": str(record.get("facility_name") or "").strip(),
		"creation": str(record.get("creation") or "").strip(),
		"creation_date": _date_only(record.get("creation")),
		"claim_status": str(record.get("claim_status") or "").strip(),
		"claim_amount": _amount(record.get("claim_amount")),
		"claim_subtype": str(record.get("claim_subtype") or "").strip(),
		"use": str(record.get("use") or "").strip(),
		"date_start": _date_only(record.get("date_start")),
		"date_end": _date_only(record.get("date_end")),
		"interventions_code": _code_list(record.get("interventions_code")),
		"interventions": _label_list(record.get("interventions")),
		"diagnoses_code": _code_list(record.get("diagnoses_code")),
		"diagnoses": _label_list(record.get("diagnoses")),
		"visit_id": str(record.get("visit_id") or "").strip(),
		"bundle_id": str(record.get("bundle_id") or "").strip(),
	}
	if isinstance(record.get("procedures"), list):
		out["procedures"] = [p for p in record["procedures"] if isinstance(p, dict)]
	return out


@dataclass
class RuleContext:
	"""The normalized per-run context the rule engine consumes."""

	cr_id: str
	sources: list = field(default_factory=list)
	errors: dict = field(default_factory=dict)
	member: dict | None = None
	eligibility: dict | None = None
	consents: list = field(default_factory=list)
	consent_by_visit: dict = field(default_factory=dict)
	claims: list = field(default_factory=list)
	preauths: list = field(default_factory=list)
	contributions: dict | None = None
	visits: list = field(default_factory=list)
	shr_by_visit: dict = field(default_factory=dict)
	registry: dict = field(default_factory=dict)
	records: dict | None = None

	@classmethod
	def build(cls, cr_id: str, sources: list, results: dict, errors: dict) -> "RuleContext":
		"""Build the context from the worker's fetch results (no DB access
		except the static registry lookup for the codes present in the data)."""
		results = results or {}
		errors = dict(errors or {})
		ctx = cls(cr_id=str(cr_id or "").strip(), sources=list(sources or []), errors=errors)

		patient = results.get("patient")
		if isinstance(patient, dict):
			ctx.member = patient

		eligibility = results.get("eligibility")
		if isinstance(eligibility, dict):
			ctx.eligibility = eligibility

		consent = results.get("consent")
		records = (consent or {}).get("records") if isinstance(consent, dict) else None
		for row in records or []:
			if not isinstance(row, dict):
				continue
			consent = {
				"name": str(row.get("name") or "").strip(),
				"client": str(row.get("client") or "").strip(),
				"visit_id": str(row.get("visit_id") or "").strip(),
				"facility_id": str(row.get("facility_id") or "").strip(),
				"consent_status": str(row.get("consent_status") or "").strip(),
				"status": str(row.get("status") or "").strip(),
				"creation": str(row.get("creation") or "").strip(),
				"creation_date": _date_only(row.get("creation")),
			}
			ctx.consents.append(consent)
			if consent["visit_id"]:
				ctx.consent_by_visit.setdefault(consent["visit_id"], consent)

		for step in ("claims", "preauth"):
			data = results.get(step)
			records = (data or {}).get("records") if isinstance(data, dict) else None
			for row in records or []:
				if not isinstance(row, dict):
					continue
				normalized = _normalize_claim(row)
				if not normalized["name"]:
					continue
				if step == "claims":
					ctx.claims.append(normalized)
				else:
					ctx.preauths.append(normalized)

		contributions = results.get("contributions")
		if isinstance(contributions, dict):
			ctx.contributions = contributions

		visits = results.get("visits")
		if isinstance(visits, list):
			ctx.visits = [v for v in visits if isinstance(v, dict)]
			for visit in ctx.visits:
				visit_id = str(visit.get("visit_id") or "").strip()
				shr = [r for r in (visit.get("shr_resources") or []) if isinstance(r, dict)]
				if visit_id and shr:
					ctx.shr_by_visit[visit_id] = shr

		records_result = results.get("records")
		if isinstance(records_result, dict):
			ctx.records = records_result

		ctx.registry = _load_registry(ctx.all_intervention_codes())
		return ctx

	def all_intervention_codes(self) -> list:
		"""Unique intervention codes present in the claims/preauths (sorted)."""
		codes: set = set()
		for record in self.claims + self.preauths:
			codes.update(record.get("interventions_code") or [])
		return sorted(codes)

	def all_diagnosis_codes(self) -> list:
		codes: set = set()
		for record in self.claims:
			codes.update(record.get("diagnoses_code") or [])
		return sorted(codes)


def _load_registry(codes: list) -> dict:
	"""The static registry rows for ``codes`` keyed by code.

	One bounded query; an empty/failed lookup yields ``{}`` (rules that need
	the registry simply skip — the static matrix is enhancement, not a
	prerequisite)."""
	if not codes:
		return {}
	try:
		rows = frappe.get_all(
			REGISTRY,
			filters={"intervention_code": ["in", codes[:200]]},
			fields=[
				"intervention_code",
				"benefit_code",
				"intervention_name",
				"sub_benefit",
				"complexity",
				"tariff",
				"facility_range",
				"payment_mechanism",
				"frequency",
				"mandatory_documents",
				"mandatory_investigations",
			],
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "rule context registry lookup")
		return {}
	out = {}
	for row in rows:
		code = str(row.get("intervention_code") or "").strip()
		if code:
			out[code] = dict(row)
	return out
