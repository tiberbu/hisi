# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""The deterministic evaluators — the rule engine's findings.

Each evaluator is a pure ``fn(ctx: RuleContext, params: dict) -> list[dict]``:
no DB, no network, no LLM, no side effects (unit-testable by construction).
A finding is a dict:

    {
      "tag_code":   str,
      "severity":   "low|medium|high",
      "title":      str,
      "detail":     str,
      "evidence":   [str],          # human lines (from the refs)
      "_refs":      [EvidenceRef],  # structured refs for verification
    }

The engine applies the tag's severity policy and evidence verification on
top; evaluators report their own default severity (the built-in tag value).
Rules are fail-soft per finding: a source that errored or a field that is
missing makes the affected check skip (an absence without data is not a
finding) — the run is never blocked on one bad record.
"""

import re
from datetime import date, datetime, timedelta, timezone

from hisi.api.ai.rules.context import RuleContext
from hisi.api.ai.rules.evidence import EvidenceRef, evidence_from

#: Default cap on findings per rule (keeps the report readable; params may
#: raise it).
DEFAULT_MAX_FINDINGS = 25

_TARIFF_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_MONTHS = {
	"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
	"JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


# ---------------------------------------------------------------------------
# shared helpers


def _ref(step: str, record_ref, field: str, value) -> EvidenceRef | None:
	return evidence_from(step, record_ref, field, value)


def _finding(tag_code: str, severity: str, title: str, detail: str, refs: list) -> dict:
	refs = [r for r in refs if r is not None]
	return {
		"tag_code": tag_code,
		"severity": severity,
		"title": title,
		"detail": detail,
		"evidence": [r.to_string() for r in refs],
		"_refs": refs,
	}


def _parse_date(value) -> date | None:
	text = str(value or "").strip()[:10]
	if not re.match(r"^\d{4}-\d{2}-\d{2}", text):
		return None
	try:
		return datetime.strptime(text, "%Y-%m-%d").date()
	except ValueError:
		return None


def _dates_overlap(a_start, a_end, b_start, b_end) -> bool:
	as_, ae = _parse_date(a_start), _parse_date(a_end)
	bs, be = _parse_date(b_start), _parse_date(b_end)
	if as_ is None or bs is None:
		return False
	ae = ae or as_
	be = be or bs
	return as_ <= be and bs <= ae


def _claim_date_label(claim: dict) -> str:
	start, end = claim.get("date_start"), claim.get("date_end")
	if start and end and start != end:
		return f"{start} to {end}"
	return start or end or "undated"


def _claim_label(claim: dict) -> str:
	return f"claim {claim.get('name') or '?'}"


def _registry_tariff(row: dict) -> float | None:
	"""The registry row's numeric tariff (``None`` when not a plain number
	or when the row is per-diem/prorated — those amounts are duration-based
	and not comparable to a claim total)."""
	row = row or {}
	mechanism = str(row.get("payment_mechanism") or "").lower()
	if any(word in mechanism for word in ("per diem", "per-diem", "per diem basis")):
		return None
	raw = str(row.get("tariff") or "").strip()
	match = _TARIFF_NUMBER_RE.match(raw)
	if not match:
		return None
	try:
		return float(match.group(0).replace(",", ""))
	except ValueError:
		return None


def _fhir_code(resource: dict, field: str) -> tuple:
	"""``(code, display)`` from a FHIR ``CodeableConcept``-ish node."""
	node = resource.get(field)
	if not isinstance(node, dict):
		return "", ""
	codings = node.get("coding")
	if isinstance(codings, list):
		for coding in codings:
			if isinstance(coding, dict) and coding.get("code"):
				return str(coding["code"]).strip(), str(coding.get("display") or "").strip()
	text = node.get("text")
	if text:
		return "", str(text).strip()
	return "", ""


def _visit_resources(ctx: RuleContext, visit_id: str, *types: str) -> list:
	resources = ctx.shr_by_visit.get(visit_id) or []
	if types:
		return [r for r in resources if r.get("resourceType") in types]
	return resources


def _cap(findings: list, params: dict) -> list:
	limit = params.get("max_findings", DEFAULT_MAX_FINDINGS)
	try:
		limit = int(limit)
	except (TypeError, ValueError):
		limit = DEFAULT_MAX_FINDINGS
	return findings[:limit]


def _claim_refs(claim: dict, fields: tuple = ("name",)) -> list:
	refs = []
	for field_name in fields:
		refs.append(_ref("claims", claim.get("name"), field_name, claim.get(field_name)))
	return refs


# ---------------------------------------------------------------------------
# the eight built-in rules


def eval_orphan_claim(ctx: RuleContext, params: dict) -> list:
	"""A claim that cannot be tied to a consented visit.

	Three shapes: (a) a resolved visit with no consent record for it;
	(b) a claim with no resolvable visit at all (no supporting visit
	evidence); (c) a claim whose consent record exists but is not an
	approved state. Skipped when the consent source errored (missing
	source → no finding by design)."""
	if "consent" in ctx.errors:
		return []
	consent_visits = set(ctx.consent_by_visit)
	findings = []
	for claim in ctx.claims:
		visit_id = claim.get("visit_id") or ""
		if visit_id and not visit_id.startswith("unknown-visitId") and visit_id in consent_visits:
			consent = ctx.consent_by_visit[visit_id]
			status = str(consent.get("consent_status") or consent.get("status") or "").strip().lower()
			if status in ("", "approved", "granted", "active", "accepted", "completed"):
				continue
			detail = (
				f"{_claim_label(claim)} (visit {visit_id}) is linked to consent record {consent['name']} "
				f"which is not in an approved state (status: {consent.get('consent_status') or consent.get('status') or 'empty'}). "
				"A claim submitted against an unapproved consent cannot be tied to a consented encounter."
			)
			refs = _claim_refs(claim, ("name", "visit_id"))
			refs.append(_ref("consent", consent["name"], "consent_status", consent.get("consent_status")))
			refs.append(_ref("consent", consent["name"], "status", consent.get("status")))
			findings.append(_finding("ORPHAN_CLAIM", "high", "Claim tied to an unapproved consent", detail, refs))
			continue
		if not visit_id or visit_id.startswith("unknown-visitId"):
			detail = (
				f"{_claim_label(claim)} ({_claim_date_label(claim)}, submitted {claim.get('creation_date') or 'unknown'}) "
				"cannot be resolved to a visit — no consent record or supporting visit evidence exists for it. "
				"A claim that cannot be tied to a consented encounter is an orphan claim."
			)
			refs = _claim_refs(claim, ("name", "creation"))
			findings.append(_finding("ORPHAN_CLAIM", "high", "Claim with no supporting visit evidence", detail, refs))
			continue
		detail = (
			f"{_claim_label(claim)} (visit {visit_id}, {_claim_date_label(claim)}) has no consent record "
			f"for that visit in the member's consent history."
		)
		refs = _claim_refs(claim, ("name", "visit_id"))
		findings.append(_finding("ORPHAN_CLAIM", "high", "Claim with no matching visit consent", detail, refs))
	return _cap(findings, params)


def eval_uncovered_service(ctx: RuleContext, params: dict) -> list:
	"""Services billed while the member has no covered scheme.

	The eligibility API result is consumed deterministically, as-is: when
	the member is not covered on any scheme, any dated claim/preauth is a
	claim outside active coverage. (Window-level checks need coverage
	dates the API does not return — those arrive with the live benefits
	surface.) Skipped when the member is covered or the source errored."""
	if "eligibility" in ctx.errors:
		return []
	eligibility = ctx.eligibility
	if eligibility is None or eligibility.get("eligible"):
		return []
	dated = [c for c in ctx.claims + ctx.preauths if c.get("date_start") or c.get("date_end")]
	if not dated:
		return []
	schemes = ", ".join(
		f"{s.get('scheme') or 'unknown scheme'} ({s.get('status') or 'no status'})"
		for s in (eligibility.get("schemes") or [])[:5]
	) or "no schemes reported"
	detail = (
		f"{len(dated)} claim/preauth record(s) carry service dates while the member is not covered "
		f"on any scheme (eligibility status: {eligibility.get('status') or 'unknown'}; {schemes}). "
		"Services billed without active coverage are discrepancies."
	)
	refs = [_ref("eligibility", "eligibility", "eligible", str(eligibility.get("eligible")))]
	refs.append(_ref("eligibility", "eligibility", "status", eligibility.get("status")))
	for record in dated[:5]:
		step = "claims" if record in ctx.claims else "preauth"
		refs.append(_ref(step, record.get("name"), "date_start", record.get("date_start")))
		refs.append(_ref(step, record.get("name"), "name", record.get("name")))
	return [_finding("UNCOVERED_SERVICE", "medium", "Services billed with no covered scheme", detail, refs)]


def eval_amount_mismatch(ctx: RuleContext, params: dict) -> list:
	"""Claimed amounts that deviate materially from the priced amount.

	Two comparisons: (a) claim total vs the benefits registry tariff for a
	claimed intervention code (only for flat-fee rows — per-diem and
	proportional tariffs are skipped by design), and (b) claim vs a
	preauthorization with the same intervention code and overlapping dates
	(the preauth is the priced benchmark the claim was supposed to follow).
	Tolerance: ``tolerance_pct`` of the benchmark (default 5%) plus
	``tolerance_abs`` KES (default 100)."""
	tolerance_pct = float(params.get("tolerance_pct", 5.0))
	tolerance_abs = float(params.get("tolerance_abs", 100.0))
	findings = []

	# (a) claim vs registry tariff
	for claim in ctx.claims:
		amount = claim.get("claim_amount")
		if amount is None:
			continue
		for code in claim.get("interventions_code") or []:
			row = ctx.registry.get(code)
			tariff = _registry_tariff(row)
			if tariff is None:
				continue
			deviation = abs(amount - tariff)
			if deviation > max(tolerance_abs, tariff * tolerance_pct / 100.0):
				name = str(row.get("intervention_name") or code)
				detail = (
					f"{_claim_label(claim)} bills {amount:,.2f} KES for {name} ({code}), but the "
					f"benefits registry prices that intervention at {tariff:,.2f} KES — a deviation "
					f"of {deviation:,.2f} KES ({deviation / tariff * 100:.1f}% of the tariff) exceeds "
					f"the {tolerance_pct:g}% tolerance."
				)
				refs = [_ref("claims", claim.get("name"), "claim_amount", f"{amount:,.2f}")]
				refs.append(_ref("registry", code, "tariff", f"{tariff:,.2f}"))
				refs.append(_ref("claims", claim.get("name"), "interventions_code", code))
				findings.append(_finding("AMOUNT_MISMATCH", "medium", f"Claim amount deviates from registry tariff ({code})", detail, refs))
				break  # one tariff finding per claim (the first priced code)

	# (b) claim vs preauthorization
	for claim in ctx.claims:
		amount = claim.get("claim_amount")
		if amount is None:
			continue
		claim_codes = set(claim.get("interventions_code") or [])
		if not claim_codes:
			continue
		for preauth in ctx.preauths:
			pre_amount = preauth.get("claim_amount")
			if pre_amount is None:
				continue
			pre_codes = set(preauth.get("interventions_code") or [])
			if not (claim_codes & pre_codes):
				continue
			if not _dates_overlap(claim.get("date_start"), claim.get("date_end"), preauth.get("date_start"), preauth.get("date_end")):
				continue
			facility_match = (not claim.get("facility") and not preauth.get("facility")) or claim.get("facility") == preauth.get("facility")
			if not facility_match:
				continue
			deviation = abs(amount - pre_amount)
			if deviation <= max(tolerance_abs, pre_amount * tolerance_pct / 100.0):
				continue
			codes = ", ".join(sorted(claim_codes & pre_codes))
			detail = (
				f"{_claim_label(claim)} bills {amount:,.2f} KES while preauthorization {preauth.get('name')} "
				f"(same interventions {codes}, overlapping dates, same facility) was priced at "
				f"{pre_amount:,.2f} KES — a deviation of {deviation:,.2f} KES."
			)
			refs = [_ref("claims", claim.get("name"), "claim_amount", f"{amount:,.2f}")]
			refs.append(_ref("preauth", preauth.get("name"), "claim_amount", f"{pre_amount:,.2f}"))
			refs.append(_ref("preauth", preauth.get("name"), "name", preauth.get("name")))
			findings.append(_finding("AMOUNT_MISMATCH", "medium", "Claim amount deviates from preauthorization price", detail, refs))
			break
	return _cap(findings, params)


def eval_missing_preauth(ctx: RuleContext, params: dict) -> list:
	"""Expensive/regulated services claimed without a preauthorization on
	file. A code is preauth-required when (a) the registry row's mandatory
	documents mention a preauthorization form, or (b) the code family is in
	``preauth_required_families`` (default: ``SHA-19`` surgical services —
	per the benefits matrix every SHA-19 procedure requires prior
	authorization). A matching preauth record (same code) on file clears
	the check."""
	families = params.get("preauth_required_families") or ["SHA-19"]
	families = [str(f).strip().upper() for f in families if str(f).strip()]
	grace_days = int(params.get("grace_days", 30))

	required: dict = {}
	for claim in ctx.claims:
		for code in claim.get("interventions_code") or []:
			row = ctx.registry.get(code)
			needs = False
			if row:
				mandatory = " ".join(
					str(row.get(k) or "") for k in ("mandatory_documents", "mandatory_investigations")
				).lower()
				if "preauthorization" in mandatory or "pre-auth" in mandatory:
					needs = True
			if any(code.startswith(family) for family in families):
				needs = True
			if needs and code not in required:
				required[code] = claim

	preauth_codes = {code for p in ctx.preauths for code in (p.get("interventions_code") or [])}
	findings = []
	for code, claim in sorted(required.items()):
		if code in preauth_codes:
			continue
		name = ""
		row = ctx.registry.get(code)
		if row:
			name = str(row.get("intervention_name") or "").strip()
		detail = (
			f"{_claim_label(claim)} claims intervention {code} ({name or 'unpriced intervention'}) "
			f"({_claim_date_label(claim)}) with no preauthorization record on file for that code. "
			"Preauthorization is required for this service."
		)
		refs = [_ref("claims", claim.get("name"), "interventions_code", code)]
		refs.append(_ref("claims", claim.get("name"), "name", claim.get("name")))
		if row:
			refs.append(_ref("registry", code, "intervention_name", row.get("intervention_name")))
		findings.append(_finding("MISSING_PREAUTH", "high", f"Claimed without preauthorization ({code})", detail, refs))
	return _cap(findings, params)


def eval_duplicate_claim(ctx: RuleContext, params: dict) -> list:
	"""Two or more claims for the same service, dates and facility —
	double-billing risk. Grouping key: sorted intervention codes +
	date_start + date_end + facility (falls back to facility name); within
	a group a finding fires when the amounts agree within tolerance (they
	price the same service) or one side is unpriced."""
	tolerance_pct = float(params.get("tolerance_pct", 1.0))
	groups: dict = {}
	for claim in ctx.claims:
		key = (
			tuple(sorted(claim.get("interventions_code") or [])),
			claim.get("date_start"),
			claim.get("date_end"),
			claim.get("facility") or claim.get("facility_name"),
		)
		groups.setdefault(key, []).append(claim)
	findings = []
	for key, group in sorted(groups.items(), key=lambda item: (item[0][1] or "", item[0][0])):
		if len(group) < 2:
			continue
		codes = ", ".join(key[0]) if key[0] else "no intervention codes"
		amounts = [c.get("claim_amount") for c in group if c.get("claim_amount") is not None]
		if len(amounts) >= 2:
			max_amount = max(amounts)
			if max_amount > 0 and (max_amount - min(amounts)) / max_amount * 100.0 > tolerance_pct:
				continue  # amounts diverge materially — likely distinct items
		names = ", ".join(c.get("name") or "?" for c in group[:4])
		detail = (
			f"{len(group)} claims ({names}) bill the same interventions ({codes}) for the same period "
			f"({key[1] or 'undated'} to {key[2] or '—'}) at the same facility — a double-billing pattern."
		)
		refs = []
		for claim in group[:3]:
			refs.append(_ref("claims", claim.get("name"), "name", claim.get("name")))
			if claim.get("claim_amount") is not None:
				refs.append(_ref("claims", claim.get("name"), "claim_amount", f"{claim.get('claim_amount'):,.2f}"))
			refs.append(_ref("claims", claim.get("name"), "date_start", claim.get("date_start")))
		findings.append(_finding("DUPLICATE_CLAIM", "high", f"{len(group)} duplicate claims for the same service", detail, refs))
	return _cap(findings, params)


def _parse_period(value) -> date | None:
	"""Best-effort parse of a ledger ``period`` string to the 1st of the
	month (``2025-10`` / ``OCT 2025`` / ``202510`` / ``10/2025`` / ``October
	2025``)."""
	text = str(value or "").strip().upper()
	if not text:
		return None
	match = re.fullmatch(r"(20\d{2})[-/]?(\d{1,2})", text)
	if match:
		year, month = int(match.group(1)), int(match.group(2))
		if 1 <= month <= 12:
			return date(year, month, 1)
	match = re.fullmatch(r"([A-Z]{3,9})[-\s]+(20\d{2})", text)
	if match:
		for prefix, month in _MONTHS.items():
			if match.group(1).startswith(prefix):
				return date(int(match.group(2)), month, 1)
	match = re.fullmatch(r"(20\d{2})(\d{2})", text)
	if match and 1 <= int(match.group(2)) <= 12:
		return date(int(match.group(1)), int(match.group(2)), 1)
	match = re.fullmatch(r"(\d{1,2})/(20\d{2})", text)
	if match and 1 <= int(match.group(1)) <= 12:
		return date(int(match.group(2)), int(match.group(1)), 1)
	return None


def eval_contribution_gap(ctx: RuleContext, params: dict) -> list:
	"""A gap in the contribution ledger longer than ``max_gap_months``
	(default 3). Gaps between consecutive parseable periods, plus the tail
	from the last payment to the run date, when longer than the threshold.
	Skipped when fewer than two periods are parseable (a short ledger is
	not evidence of a gap)."""
	max_gap_months = int(params.get("max_gap_months", 3))
	threshold_days = max_gap_months * 31
	ledger = (ctx.contributions or {}).get("contributions") or []
	dated = []
	for entry in ledger:
		if not isinstance(entry, dict):
			continue
		day = _parse_period(entry.get("period"))
		if day is None:
			continue
		dated.append((day, entry))
	if len(dated) < 2:
		return []
	dated.sort(key=lambda item: item[0])
	today = datetime.now(timezone.utc).date()
	findings = []
	for (day_a, entry_a), (day_b, entry_b) in zip(dated, dated[1:]):
		gap_days = (day_b - day_a).days
		if gap_days > threshold_days:
			ref_a = _ref("contributions", entry_a.get("payment_reference"), "period", entry_a.get("period"))
			ref_b = _ref("contributions", entry_b.get("payment_reference"), "period", entry_b.get("period"))
			detail = (
				f"Contribution ledger gap of {gap_days} days ({gap_days // 30} months) between payment "
				f"{entry_a.get('payment_reference') or 'the prior payment'} ({entry_a.get('period')}) and "
				f"payment {entry_b.get('payment_reference') or 'the later payment'} ({entry_b.get('period')}) — "
				f"beyond the {max_gap_months}-month tolerance."
			)
			findings.append(_finding("CONTRIBUTION_GAP", "medium", "Gap in the contribution ledger", detail, [ref_a, ref_b]))
	last_day, last_entry = dated[-1]
	if (today - last_day).days > threshold_days and last_day <= today:
		ref = _ref("contributions", last_entry.get("payment_reference"), "period", last_entry.get("period"))
		detail = (
			f"The last recorded contribution ({last_entry.get('payment_reference') or 'the final ledger entry'}, "
			f"period {last_entry.get('period')}) is {(today - last_day).days} days old — beyond the "
			f"{max_gap_months}-month tolerance with no newer payment on the ledger."
		)
		findings.append(_finding("CONTRIBUTION_GAP", "medium", "No recent contribution on the ledger", detail, [ref]))
	return _cap(findings, params)


def eval_timing_anomaly(ctx: RuleContext, params: dict) -> list:
	"""Dates out of logical order: a service dated after the claim's
	submission, a service dated in the future, a preauthorization created
	after a claim for the same code, or a contribution posted for a future
	period."""
	today = datetime.now(timezone.utc).date() + timedelta(days=1)
	findings = []
	for claim in ctx.claims:
		start = _parse_date(claim.get("date_start"))
		created = _parse_date(claim.get("creation"))
		if start and created and start > created:
			detail = (
				f"{_claim_label(claim)} was submitted on {created} but its service starts on {start} — "
				"the service postdates the submission."
			)
			refs = [_ref("claims", claim.get("name"), "creation", claim.get("creation"))]
			refs.append(_ref("claims", claim.get("name"), "date_start", claim.get("date_start")))
			findings.append(_finding("TIMING_ANOMALY", "low", "Service dated after claim submission", detail, refs))
		if start and start > today:
			detail = (
				f"{_claim_label(claim)} is dated {start} to {claim.get('date_end') or start} — service dates "
				"in the future relative to this analysis."
			)
			refs = [_ref("claims", claim.get("name"), "date_start", claim.get("date_start"))]
			findings.append(_finding("TIMING_ANOMALY", "low", "Service dated in the future", detail, refs))
	for preauth in ctx.preauths:
		pre_created = _parse_date(preauth.get("creation"))
		if not pre_created:
			continue
		pre_codes = set(preauth.get("interventions_code") or [])
		for claim in ctx.claims:
			if not (pre_codes & set(claim.get("interventions_code") or [])):
				continue
			claim_created = _parse_date(claim.get("creation"))
			if claim_created and claim_created <= pre_created:
				detail = (
					f"Preauthorization {preauth.get('name')} (created {preauth.get('creation_date')}) postdates "
					f"claim {claim.get('name')} (created {claim.get('creation_date')}) for the same interventions "
					"— the authorization came after the claim it was supposed to precede."
				)
				refs = [_ref("preauth", preauth.get("name"), "creation", preauth.get("creation"))]
				refs.append(_ref("claims", claim.get("name"), "creation", claim.get("creation")))
				findings.append(_finding("TIMING_ANOMALY", "low", "Preauthorization postdates the claim", detail, refs))
				break
	for entry in (ctx.contributions or {}).get("contributions") or []:
		if not isinstance(entry, dict):
			continue
		day = _parse_period(entry.get("period"))
		if day and day > today:
			ref = _ref("contributions", entry.get("payment_reference"), "period", entry.get("period"))
			detail = (
				f"Contribution {entry.get('payment_reference') or 'on the ledger'} is posted for period "
				f"{entry.get('period')} — a future period."
			)
			findings.append(_finding("TIMING_ANOMALY", "low", "Contribution posted for a future period", detail, [ref]))
	return _cap(findings, params)


def eval_eligibility_mismatch(ctx: RuleContext, params: dict) -> list:
	"""Claims or preauthorizations submitted while the member is inactive /
	uncovered. The eligibility API result is consumed deterministically,
	as-is (no model analysis of eligibility): when the member is not
	eligible and the member has claims/preauths, that is a mismatch —
	one finding citing the eligibility state and the affected records."""
	if "eligibility" in ctx.errors:
		return []
	eligibility = ctx.eligibility
	if eligibility is None or eligibility.get("eligible"):
		return []
	affected = ctx.claims + ctx.preauths
	if not affected:
		return []
	schemes = "; ".join(
		f"{s.get('scheme') or 'unknown'}: {s.get('status') or s.get('message') or 'no status'}"
		for s in (eligibility.get("schemes") or [])[:4]
	) or "no scheme detail returned"
	status = str(eligibility.get("status") or "unknown")
	detail = (
		f"{len(affected)} claim/preauth record(s) exist while the member is not eligible "
		f"(status: {status}). {schemes}. Services submitted by an "
		f"inactive or uncovered member are discrepancies."
	)
	refs = [_ref("eligibility", "eligibility", "eligible", str(eligibility.get("eligible")))]
	refs.append(_ref("eligibility", "eligibility", "status", eligibility.get("status")))
	for record in affected[:4]:
		step = "claims" if record in ctx.claims else "preauth"
		refs.append(_ref(step, record.get("name"), "name", record.get("name")))
	return [_finding("ELIGIBILITY_MISMATCH", "high", "Claims submitted while the member is not eligible", detail, refs)]


# ---------------------------------------------------------------------------
# clinical <-> claim triangulation (Phase B)


def eval_tri_diagnosis(ctx: RuleContext, params: dict) -> list:
	"""Diagnosis triangulation: the claim's diagnosis codes vs the
	Condition resources in the claim's visit SHR bundle. A claim asserting
	diagnosis codes that the visit's clinical record documents none of is a
	disagreement with structured evidence on both sides. Skipped when the
	visit has no Condition resources (nothing to compare against) or the
	claim has no diagnosis codes."""
	findings = []
	for claim in ctx.claims:
		diag_codes = [c.upper() for c in (claim.get("diagnoses_code") or []) if c]
		if not diag_codes:
			continue
		visit_id = claim.get("visit_id") or ""
		if not visit_id or visit_id.startswith("unknown-visitId"):
			continue
		condition_codes = []
		for index, resource in enumerate(_visit_resources(ctx, visit_id, "Condition")):
			code, _display = _fhir_code(resource, "code")
			if code:
				condition_codes.append((index, code.upper(), _display or code))
		if not condition_codes:
			continue
		shr_codes = {code for _i, code, _d in condition_codes}
		if set(diag_codes) & shr_codes:
			continue
		detail = (
			f"{_claim_label(claim)} asserts diagnosis code(s) {', '.join(diag_codes[:4])}, but the "
			f"clinical record for visit {visit_id} documents no condition matching those codes "
			f"(documented: {', '.join(sorted(shr_codes)[:4])})."
		)
		refs = [_ref("claims", claim.get("name"), "diagnoses_code", ", ".join(diag_codes[:4]))]
		first_index, first_code, first_display = condition_codes[0]
		refs.append(_ref("shr", f"{visit_id}#Condition#{first_index}", "code", first_code))
		findings.append(_finding("TRI_DIAGNOSIS", "medium", "Claim diagnosis not documented in the clinical record", detail, refs))
	return _cap(findings, params)


def eval_tri_intervention(ctx: RuleContext, params: dict) -> list:
	"""Intervention triangulation: a surgical claim (``SHA-19`` family)
	whose visit SHR bundle contains no Procedure or ServiceRequest
	resource — the claimed procedure has no clinical documentation in the
	visit record. (Non-surgical claims are skipped: many services leave no
	procedure-shaped resource, so only the surgical family is provable.)"""
	findings = []
	for claim in ctx.claims:
		surgical = [c for c in (claim.get("interventions_code") or []) if c.startswith("SHA-19")]
		if not surgical:
			continue
		visit_id = claim.get("visit_id") or ""
		if not visit_id or visit_id.startswith("unknown-visitId"):
			continue
		if _visit_resources(ctx, visit_id, "Procedure", "ServiceRequest"):
			continue
		detail = (
			f"{_claim_label(claim)} claims surgical intervention(s) {', '.join(surgical[:4])} for visit "
			f"{visit_id}, but the visit's clinical record contains no Procedure or ServiceRequest "
			"resource documenting the procedure."
		)
		refs = [_ref("claims", claim.get("name"), "interventions_code", ", ".join(surgical[:4]))]
		refs.append(_ref("claims", claim.get("name"), "name", claim.get("name")))
		findings.append(_finding("TRI_INTERVENTION", "medium", "Claimed surgery with no procedure in the clinical record", detail, refs))
	return _cap(findings, params)


def eval_tri_dates(ctx: RuleContext, params: dict) -> list:
	"""Date triangulation: the claim's service dates vs the Encounter
	period recorded in the visit's SHR bundle. The claim window must sit
	inside the encounter window up to ``tolerance_days`` (default 1) on
	each side."""
	tolerance = int(params.get("tolerance_days", 1))
	findings = []
	for claim in ctx.claims:
		visit_id = claim.get("visit_id") or ""
		if not visit_id or visit_id.startswith("unknown-visitId"):
			continue
		claim_start = _parse_date(claim.get("date_start"))
		claim_end = _parse_date(claim.get("date_end")) or claim_start
		for index, encounter in enumerate(_visit_resources(ctx, visit_id, "Encounter")):
			period = encounter.get("period")
			if not isinstance(period, dict):
				continue
			enc_start = _parse_date(period.get("start"))
			enc_end = _parse_date(period.get("end"))
			if enc_start and claim_start and claim_start < enc_start - timedelta(days=tolerance):
				detail = (
					f"{_claim_label(claim)} bills service starting {claim_start}, before the encounter "
					f"recorded for visit {visit_id} began on {enc_start}."
				)
				refs = [_ref("claims", claim.get("name"), "date_start", claim.get("date_start"))]
				refs.append(_ref("shr", f"{visit_id}#Encounter#{index}", "period.start", period.get("start")))
				findings.append(_finding("TRI_DATES", "medium", "Claim service predates the recorded encounter", detail, refs))
				break
			if enc_end and claim_end and claim_end > enc_end + timedelta(days=tolerance):
				detail = (
					f"{_claim_label(claim)} bills service ending {claim_end}, after the encounter "
					f"recorded for visit {visit_id} ended on {enc_end}."
				)
				refs = [_ref("claims", claim.get("name"), "date_end", claim.get("date_end"))]
				refs.append(_ref("shr", f"{visit_id}#Encounter#{index}", "period.end", period.get("end")))
				findings.append(_finding("TRI_DATES", "medium", "Claim service extends beyond the recorded encounter", detail, refs))
				break
	return _cap(findings, params)


def eval_tri_facility(ctx: RuleContext, params: dict) -> list:
	"""Facility triangulation: the claim's facility vs the facility on the
	consent record for the same visit. Skipped when either side lacks a
	facility value (an absence is not a mismatch)."""
	findings = []
	for claim in ctx.claims:
		visit_id = claim.get("visit_id") or ""
		if not visit_id or visit_id.startswith("unknown-visitId"):
			continue
		consent = ctx.consent_by_visit.get(visit_id)
		if not consent:
			continue
		claim_facility = (claim.get("facility") or "").strip()
		consent_facility = (consent.get("facility_id") or "").strip()
		if not claim_facility or not consent_facility:
			continue
		if claim_facility.lower() == consent_facility.lower():
			continue
		detail = (
			f"{_claim_label(claim)} was submitted at facility {claim_facility}, but the consent record "
			f"for visit {visit_id} ({consent.get('name')}) names facility {consent_facility}."
		)
		refs = [_ref("claims", claim.get("name"), "facility", claim_facility)]
		refs.append(_ref("consent", consent.get("name"), "facility_id", consent_facility))
		findings.append(_finding("TRI_FACILITY", "medium", "Claim facility differs from the consented facility", detail, refs))
	return _cap(findings, params)
