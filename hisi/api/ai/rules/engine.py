# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""The rule engine orchestrator — deterministic findings, verified.

``run_rules`` executes the enabled tags' evaluators against the run's
``RuleContext``, verifies every finding's structured evidence against the
fetched data (unverifiable findings are quarantined, never silently
dropped — they land in the job's provenance audit), applies the tag
severity policy, and shapes the result into the same contract the rest of
the pipeline already consumes (``{summary, executive_summary, overall,
findings}``) so the markdown/PDF renderers work unchanged.

The deterministic executive summary is always computed (template from the
findings + stats); the AI narrative stage (when the mode uses one) may
refine it but never replaces findings.
"""

import json
from datetime import datetime

import frappe

from hisi.api.ai.rules import evaluators as ev
from hisi.api.ai.rules.evidence import verify_findings
from hisi.api.ai.rules.context import RuleContext

#: Bump when the built-in rule catalog changes (stamped on every job).
CATALOG_VERSION = "3.0"

#: The AI modes (per-run or defaulted in ``HISI Report Settings``).
AI_MODES = (
	"deterministic_only",
	"deterministic_plus_narrative",
	"autonomous_full",
	"legacy_llm",
)

#: rule_key -> evaluator. The tag's ``rule_key`` (or its tag code,
#: lower-cased) selects the evaluator; unknown keys are skipped with a
#: warning (a typo must not sink a run).
EVALUATORS = {
	"orphan_claim": ev.eval_orphan_claim,
	"uncovered_service": ev.eval_uncovered_service,
	"amount_mismatch": ev.eval_amount_mismatch,
	"missing_preauth": ev.eval_missing_preauth,
	"duplicate_claim": ev.eval_duplicate_claim,
	"timing_anomaly": ev.eval_timing_anomaly,
	"eligibility_mismatch": ev.eval_eligibility_mismatch,
	"tri_diagnosis": ev.eval_tri_diagnosis,
	"tri_intervention": ev.eval_tri_intervention,
	"tri_dates": ev.eval_tri_dates,
	"tri_facility": ev.eval_tri_facility,
	"claim_without_shr_data": ev.eval_claim_without_shr_data,
	"shr_data_without_claim": ev.eval_shr_data_without_claim,
	"claim_not_dispatched": ev.eval_claim_not_dispatched,
	"cross_facility_duplicate": ev.eval_cross_facility_duplicate,
	"consent_after_service": ev.eval_consent_after_service,
	"preauth_period_mismatch": ev.eval_preauth_period_mismatch,
	"medication_not_documented": ev.eval_medication_not_documented,
	# Catalog 3.0 — Tier A clinical consistency (data available today).
	"claim_before_birth": ev.eval_claim_before_birth,
	"duplicate_serviced": ev.eval_duplicate_serviced,
	"claim_without_diagnosis": ev.eval_claim_without_diagnosis,
	"inverted_service_window": ev.eval_inverted_service_window,
}

SEVERITIES = ("low", "medium", "high")
OVERALL_LEVELS = ("clean", "minor", "major")


def _tag_get(tag, key, default=""):
	"""Field access for a tag snapshot row — the snapshot is stored as
	JSON dicts, but callers may also hand over object rows; handle both."""
	if isinstance(tag, dict):
		value = tag.get(key)
	else:
		value = getattr(tag, key, None)
	return default if value in (None, "") else value


def resolve_rule_key(tag) -> str:
	"""The evaluator key for one tag row (``rule_key`` or tag code lower)."""
	key = str(_tag_get(tag, "rule_key", "")).strip()
	if key:
		return key
	return str(_tag_get(tag, "tag_code", "")).strip().lower()


def _load_parameters(raw) -> dict:
	if raw in (None, ""):
		return {}
	if isinstance(raw, dict):
		return dict(raw)
	try:
		parsed = json.loads(raw)
		return parsed if isinstance(parsed, dict) else {}
	except (json.JSONDecodeError, ValueError):
		return {}


def _apply_severity(finding: dict, tag, overrides: dict) -> str:
	"""The tag's severity policy: ``fixed`` (the tag's severity, default),
	``computed`` (the evaluator's own value), ``policy_table`` (the
	``severity_overrides`` map by tag code, falling back to the tag's)."""
	policy = str(_tag_get(tag, "severity_policy", "fixed")).strip().lower()
	code = str(_tag_get(tag, "tag_code", ""))
	if policy == "policy_table" and code in overrides:
		return str(overrides[code]).strip().lower()
	if policy == "computed":
		severity = str(finding.get("severity") or "medium").lower()
		return severity if severity in SEVERITIES else "medium"
	severity = str(_tag_get(tag, "severity", "medium")).lower()
	return severity if severity in SEVERITIES else "medium"


def _recompute_overall(findings: list) -> str:
	if not findings:
		return "clean"
	if any(f["severity"] in ("medium", "high") for f in findings):
		return "major"
	return "minor"


def _period_label(claims: list) -> str:
	dates = []
	for claim in claims:
		for key in ("date_start", "date_end"):
			day = str(claim.get(key) or "").strip()[:10]
			if len(day) == 10:
				dates.append(day)
	if not dates:
		return ""
	return f"from {min(dates)} to {max(dates)}"


def deterministic_summary(findings: list, ctx: RuleContext, sources: list) -> dict:
	"""The template executive summary (always available — the AI stage may
	refine it but the report is never blocked on the model)."""
	counts = {"high": 0, "medium": 0, "low": 0}
	for finding in findings:
		severity = finding.get("severity")
		counts[severity if severity in counts else "medium"] += 1
	period = _period_label(ctx.claims)
	# Contributions are context only (the payer makes contribution decisions):
	# they are never listed among the sources the analysis audited.
	analyzed = [s for s in sources if s != "contributions"]
	source_label = ", ".join(analyzed) if analyzed else "the configured sources"
	if not findings:
		summary = f"No discrepancies identified across {source_label} for this member."
		executive = (
			f"The analysis audited this member's data ({source_label}"
			+ (f" for the period {period})" if period else ")")
			+ " and found no discrepancies supported by the data under the "
			"configured checks. All cited figures were verified against the "
			"fetched records before this report was produced."
		)
		return {"summary": summary, "executive_summary": executive}
	top = [f for f in findings if f["severity"] == "high"] or findings
	highlights = "; ".join(f["title"] for f in top[:3])
	summary = (
		f"{len(findings)} discrepancies identified "
		+ (f"({counts['high']} high, {counts['medium']} medium, {counts['low']} low) " if findings else "")
		+ f"across {source_label}."
	)
	amounts = [c.get("claim_amount") for c in ctx.claims if c.get("claim_amount") is not None]
	amount_clause = (
		f" The claims under review total {sum(amounts):,.0f} KES." if amounts else ""
	)
	executive = (
		f"This analysis audited the member's data ({source_label}"
		+ (f" for the period {period})" if period else ")")
		+ f" using the configured discrepancy checks. {len(findings)} discrepancy(ies) were "
		+ f"identified, including: {highlights}."
		+ f" {counts['high']} finding(s) are high severity."
		+ amount_clause
		+ " Every finding above carries structured evidence that was re-verified "
		"against the fetched records at build time; findings that could not be "
		"verified were excluded. The recommended next step is to review the "
		"high-severity findings first and confirm or correct the underlying "
		"records before any payment decision."
	)
	return {"summary": summary, "executive_summary": executive}


def run_rules(ctx: RuleContext, tags: list, report_settings=None) -> dict:
	"""Run the deterministic pipeline over one run's context.

	``tags``: the tag snapshot rows (dicts with ``tag_code``, ``label``,
	``severity``, ``rule_type``, ``rule_key``, ``parameters``,
	``severity_policy``). ``report_settings``: the ``HISI Report Settings``
	mapping (``evidence_policy``, ``severity_overrides``). Returns
	``{"result", "provenance", "quarantined", "warnings"}`` where
	``result`` is the normalized contract payload.
	"""
	settings = report_settings or {}
	evidence_policy = str(settings.get("evidence_policy") or "exclude").strip().lower()
	if evidence_policy not in ("exclude", "mark"):
		evidence_policy = "exclude"
	try:
		overrides = json.loads(settings.get("severity_overrides") or "{}")
		if not isinstance(overrides, dict):
			overrides = {}
	except (json.JSONDecodeError, ValueError, TypeError):
		overrides = {}

	raw_findings: list = []
	warnings: list = []
	per_tag: dict = {}
	for tag in tags or []:
		if not isinstance(tag, dict):
			continue
		rule_type = str(tag.get("rule_type") or "code").strip().lower()
		if rule_type in ("llm", "off"):
			continue  # not a deterministic rule — the rule engine skips it
		code = str(_tag_get(tag, "tag_code", "")).strip()
		key = str(_tag_get(tag, "rule_key", "")).strip() or code.lower()
		evaluator = EVALUATORS.get(key)
		if evaluator is None:
			warnings.append(f"tag {code}: no deterministic evaluator for rule key {key!r} — skipped")
			continue
		params = _load_parameters(tag.get("parameters"))
		try:
			found = evaluator(ctx, params)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"rule evaluator {key}")
			warnings.append(f"tag {code}: evaluator {key} failed — skipped (other rules unaffected)")
			continue
		for finding in found:
			finding = dict(finding)
			finding["severity"] = _apply_severity(finding, tag, overrides)
			raw_findings.append(finding)
		per_tag[code] = len(found)

	kept, quarantined = verify_findings(raw_findings, ctx, evidence_policy)

	# Public shape (the stored/contract form) — internal keys stay out.
	public_findings = []
	provenance = []
	for finding in kept:
		public_findings.append(
			{
				"tag_code": finding.get("tag_code", ""),
				"severity": finding.get("severity", "medium"),
				"title": finding.get("title", ""),
				"detail": finding.get("detail", ""),
				"evidence": [str(e) for e in (finding.get("evidence") or []) if str(e).strip()],
				# M13 per-finding verdict: analyst override (valid / false_positive /
				# unknown). Unknown by default; set via set_finding_verdict and stored
				# back into this payload with the overrider's user id.
				"verdict": str(finding.get("verdict") or "unknown").strip().lower() or "unknown",
				"verdict_updated_by": finding.get("verdict_updated_by"),
			}
		)
		provenance.append(
			{
				"tag_code": finding.get("tag_code", ""),
				"title": finding.get("title", ""),
				"severity": finding.get("severity", "medium"),
				"source": "rule",
				"catalog_version": CATALOG_VERSION,
				"verified": True,
				"evidence_refs": [ref.to_dict() for ref in (finding.get("_refs") or [])],
			}
		)
	for finding in quarantined:
		provenance.append(
			{
				"tag_code": finding.get("tag_code", ""),
				"title": finding.get("title", ""),
				"severity": finding.get("severity", "medium"),
				"source": "rule",
				"catalog_version": CATALOG_VERSION,
				"verified": False,
				"verification": finding.get("_verification"),
				"note": "quarantined by evidence verification (policy: %s); not in the deliverable" % evidence_policy,
				"evidence_refs": [ref.to_dict() for ref in (finding.get("_refs") or [])],
			}
		)

	normalized = deterministic_summary(public_findings, ctx, ctx.sources)
	normalized["overall"] = _recompute_overall(public_findings)
	normalized["findings"] = public_findings

	return {
		"result": normalized,
		"provenance": provenance,
		"quarantined": len(quarantined),
		"warnings": warnings,
		"per_tag": per_tag,
	}
