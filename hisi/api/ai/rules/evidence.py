# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""Structured evidence + verification — the "verify, don't trust" gate.

Every finding produced by the rule engine carries ``EvidenceRef`` objects:
``{step, record_ref, field, quoted_value}``. :func:`verify_ref` resolves
the reference inside the run's ``RuleContext`` and compares the quoted
value against the actual fetched value (date/number/trim normalization),
so a finding can only reach a deliverable with evidence that provably
exists in the data.

Verification outcomes:

- ``verified`` — the record and field exist and the value matches.
- ``unverified`` — the record exists but the value does not match (a rule
  bug or a data shape surprise — the finding is quarantined by policy).
- ``missing_source`` — the source step errored or the record is absent
  (the finding is quarantined: an absence claim without the source data
  present is not provable).

Registry evidence (``step == "registry"``) resolves against the static
registry snapshot on the context, and SHR evidence
(``record_ref == "<visit_id>#<ResourceType>#<index>"``) resolves into the
per-visit FHIR resources with a dotted field path.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

from hisi.api.ai.rules.context import RuleContext

VERIFIED = "verified"
UNVERIFIED = "unverified"
MISSING_SOURCE = "missing_source"

_NUMBER_RE = re.compile(r"^-?[\d,]+\.?\d*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


@dataclass
class EvidenceRef:
	"""One structured evidence citation: where a value was read from."""

	step: str
	record_ref: str
	field: str
	quoted_value: str

	def to_dict(self) -> dict:
		return {
			"step": self.step,
			"record_ref": self.record_ref,
			"field": self.field,
			"quoted_value": self.quoted_value,
		}

	def to_string(self) -> str:
		"""The human line the report's evidence list shows."""
		if self.step == "registry":
			return f"benefits registry {self.record_ref}.{self.field} = {self.quoted_value}"
		if self.step == "eligibility":
			return f"eligibility.{self.field} = {self.quoted_value}"
		if self.step == "contributions":
			return f"contributions ledger {self.record_ref}.{self.field} = {self.quoted_value}"
		if self.step == "shr":
			visit_id, _, rest = self.record_ref.partition("#")
			return f"visit {visit_id} {rest}.{self.field} = {self.quoted_value}"
		return f"{self.step}:{self.record_ref}.{self.field} = {self.quoted_value}"


def evidence_from(step: str, record_ref, field: str, value) -> EvidenceRef | None:
	"""Build a ref from a raw value; ``None`` when the value is empty
	(an empty citation is never evidence)."""
	text = str(value or "").strip()
	if not text:
		return None
	ref = str(record_ref or "").strip()
	if not ref or not str(field or "").strip():
		return None
	return EvidenceRef(step=step, record_ref=ref, field=str(field).strip(), quoted_value=text)


def _normalize_number(value):
	text = str(value).replace(",", "").strip()
	if not _NUMBER_RE.match(text):
		return None
	try:
		return float(text)
	except ValueError:
		return None


def _normalize_date(value) -> date | None:
	text = str(value or "").strip()
	if not _DATE_RE.match(text):
		return None
	try:
		return datetime.strptime(text[:10], "%Y-%m-%d").date()
	except ValueError:
		return None


def _values_match(quoted: str, actual) -> bool:
	"""Value comparison with date/number/trim normalization.

	List-valued fields (e.g. a claim's ``interventions_code``) match when the
	quoted value is one of the elements or the full joined list — a citation
	of one code in a multi-code field is still verifiable."""
	if isinstance(actual, (list, tuple)):
		items = [str(a).strip() for a in actual]
		joined = ", ".join(items)
		if quoted_text := str(quoted).strip():
			if quoted_text in items or quoted_text.lower() == joined.lower():
				return True
			if quoted_text.lower() in {item.lower() for item in items}:
				return True
		return False
	actual_text = str(actual).strip() if actual is not None else ""
	quoted_text = str(quoted).strip()
	if quoted_text == actual_text:
		return True
	q_num, a_num = _normalize_number(quoted_text), _normalize_number(actual_text)
	if q_num is not None and a_num is not None:
		return abs(q_num - a_num) < 0.01
	q_date, a_date = _normalize_date(quoted_text), _normalize_date(actual_text)
	if q_date is not None and a_date is not None:
		return q_date == a_date
	return quoted_text.lower() == actual_text.lower()


def _get_field(record, fieldpath: str):
	"""Resolve a dotted field path inside a (possibly nested) dict."""
	node = record
	for part in fieldpath.split("."):
		if isinstance(node, dict):
			node = node.get(part)
		else:
			return None
	return node


def _resolve_record(ctx: RuleContext, step: str, record_ref: str):
	"""Resolve the source record for a ref.

	Returns ``(record, found)`` — ``record`` is the dict (or the eligibility
	/single object) and ``found`` says whether it exists in the context."""
	if step in ("claims", "preauth"):
		source = ctx.claims if step == "claims" else ctx.preauths
		for record in source:
			if record.get("name") == record_ref:
				return record, True
		return None, False
	if step == "consent":
		for record in ctx.consents:
			if record.get("name") == record_ref:
				return record, True
		return None, False
	if step == "eligibility":
		if ctx.eligibility is None:
			return None, False
		return ctx.eligibility, True
	if step == "patient":
		if ctx.member is None:
			return None, False
		return ctx.member, True
	if step == "contributions":
		if ctx.contributions is None:
			return None, False
		entries = ctx.contributions.get("contributions") or []
		for entry in entries:
			if str(entry.get("payment_reference") or "").strip() == record_ref:
				return entry, True
		return None, False
	if step == "registry":
		row = ctx.registry.get(record_ref)
		return (row, True) if row else (None, False)
	if step == "shr":
		visit_id, _, rest = record_ref.partition("#")
		rtype, _, index = rest.partition("#")
		resources = ctx.shr_by_visit.get(visit_id) or []
		# Index = the n-th resource of that type at the visit (not the
		# position in the full per-visit list).
		count = 0
		for resource in resources:
			if str(resource.get("resourceType") or "") != rtype:
				continue
			if str(count) == index:
				return resource, True
			count += 1
		return None, False
	return None, False


def verify_ref(ref: EvidenceRef, ctx: RuleContext) -> str:
	"""Verify one evidence reference against the context (see module doc).

	A source that errored is handled implicitly: none of its records exist
	in the context, so its refs resolve to ``MISSING_SOURCE``."""
	record, found = _resolve_record(ctx, ref.step, ref.record_ref)
	if not found:
		return MISSING_SOURCE
	actual = _get_field(record, ref.field)
	if actual in (None, ""):
		return MISSING_SOURCE
	return VERIFIED if _values_match(ref.quoted_value, actual) else UNVERIFIED


def verify_findings(findings: list, ctx: RuleContext, policy: str = "exclude") -> tuple:
	"""Verify every finding's evidence refs against the context.

	Returns ``(kept, quarantined)``. ``policy``: ``"exclude"`` drops any
	finding with a non-verified ref (the default — a compliance deliverable
	cannot carry unverifiable citations); ``"mark"`` keeps it with an
	``evidence_verified: False`` stamp and a note in the detail.
	"""
	kept = []
	quarantined = []
	for finding in findings:
		refs = finding.get("_refs") or []
		if not refs:
			kept.append(finding)
			continue
		outcomes = [verify_ref(ref, ctx) for ref in refs]
		if all(outcome == VERIFIED for outcome in outcomes):
			kept.append(finding)
		else:
			finding["_verification"] = outcomes
			if policy == "mark":
				finding["evidence_verified"] = False
				finding["detail"] = (
					str(finding.get("detail") or "")
					+ " [Evidence could not be fully re-verified against the fetched data; "
					"treat the citations with caution.]"
				).strip()
				kept.append(finding)
			else:
				quarantined.append(finding)
	return kept, quarantined
