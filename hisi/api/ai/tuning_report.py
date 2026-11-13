# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""M13.3 tuning report — a one-page markdown over the last N report runs.

Manual first (``bench --site hisi-local execute hisi.api.ai.tuning_report.build_tuning_report``);
cron-able later. It aggregates the measurements the pipeline now stores:

- findings per rule (how often each tag fires),
- verdict distribution per rule (analyst overrides from M13.2 — the raw
  material for severity overrides, rule params, and rule retirement),
- narrative fallback rate (G6: how often the narrative was skipped/rejected
  and the deterministic summary stood in),
- coverage error rate (G9: per-lane data availability across runs).

No LLM, no network — pure aggregation over stored report documents.
"""

import json

import frappe

REPORT = "HISI Discrepancy Report"


def _load_reports(last_n: int) -> list:
	rows = frappe.get_all(
		REPORT,
		filters={"status": "Success"},
		fields=["name", "creation", "findings", "findings_provenance", "coverage", "report", "model"],
		order_by="creation desc",
		limit=max(1, int(last_n or 30)),
	)
	return [dict(r) for r in rows]


def _json(blob):
	if not blob:
		return None
	try:
		return json.loads(blob)
	except (json.JSONDecodeError, ValueError, TypeError):
		return None


def build_tuning_report(last_n: int = 30, write_file: str | None = None) -> str:
	"""Aggregate the last N successful reports into the one-page markdown."""
	reports = _load_reports(last_n)
	if not reports:
		return "# Report pipeline tuning report\n\nNo successful reports to aggregate."

	findings_per_rule: dict = {}
	verdicts_per_rule: dict = {}
	narrative_fallback = 0
	coverage_total = 0
	coverage_errors = {}
	coverage_empty = {}

	for row in reports:
		parsed = _json(row.get("findings"))
		# The stored payload is the contract dict {summary, overall, findings: [...]}.
		if isinstance(parsed, dict):
			findings = parsed.get("findings") or []
		elif isinstance(parsed, list):
			findings = parsed
		else:
			findings = []
		for f in findings:
			if not isinstance(f, dict):
				continue
			code = str(f.get("tag_code") or "?").strip()
			findings_per_rule[code] = findings_per_rule.get(code, 0) + 1
			v = str(f.get("verdict") or "unknown").strip().lower() or "unknown"
			verdicts_per_rule.setdefault(code, {})
			verdicts_per_rule[code][v] = verdicts_per_rule[code].get(v, 0) + 1
		# G6: the narrative fallback note is the amber line under the executive
		# summary in the stored markdown.
		md = str(row.get("report") or "")
		# G6: the fallback renders as an italic line (markdown) / amber note (PDF)
		# carrying the stage warning text.
		if "narrative stage skipped" in md or "narrative rejected" in md:
			narrative_fallback += 1
		coverage = _json(row.get("coverage"))
		if isinstance(coverage, dict):
			for lane in coverage.get("lanes") or []:
				if not isinstance(lane, dict):
					continue
				key = str(lane.get("key") or "?")
				coverage_total += 1
				if lane.get("status") == "error":
					coverage_errors[key] = coverage_errors.get(key, 0) + 1
				elif lane.get("status") == "empty":
					coverage_empty[key] = coverage_empty.get(key, 0) + 1

	total_findings = sum(findings_per_rule.values())
	lines = [
		"# Report pipeline tuning report",
		"",
		f"Last {len(reports)} successful report(s), {total_findings} finding(s).",
		"",
		"## Findings per rule",
		"",
	]
	if findings_per_rule:
		lines += ["| Rule | Findings | % of total |", "| --- | ---: | ---: |"]
		for code in sorted(findings_per_rule, key=lambda c: -findings_per_rule[c]):
			n = findings_per_rule[code]
			lines.append(f"| {code} | {n} | {n / total_findings:.0%} |")
	else:
		lines.append("_No findings._")

	lines += ["", "## Verdict distribution per rule (analyst overrides)", ""]
	if any(verdicts_per_rule.values()):
		lines += ["| Rule | valid | false_positive | unknown |", "| --- | ---: | ---: | ---: |"]
		for code in sorted(verdicts_per_rule):
			v = verdicts_per_rule[code]
			lines.append(f"| {code} | {v.get('valid', 0)} | {v.get('false_positive', 0)} | {v.get('unknown', 0)} |")
	else:
		lines.append("_No verdicts recorded yet (the M13 verdict API has not been used)._")

	lines += ["", "## Reliability", ""]
	fallback_rate = narrative_fallback / len(reports)
	lines.append(f"- Narrative fallback rate: **{narrative_fallback}/{len(reports)}** ({fallback_rate:.0%}) — G6's amber note fired.")
	if coverage_total:
		err_rate = sum(coverage_errors.values()) / coverage_total
		lines.append(f"- Coverage error rate: **{sum(coverage_errors.values())}/{coverage_total}** lane-instances ({err_rate:.0%}) — G9.")
		if coverage_errors:
			lines.append("- Erroring lanes: " + ", ".join(f"{k} ({v})" for k, v in sorted(coverage_errors.items())))
		if coverage_empty:
			lines.append("- Empty lanes: " + ", ".join(f"{k} ({v})" for k, v in sorted(coverage_empty.items())))
	else:
		lines.append("- Coverage: no coverage data stored (older runs predate G9).")
	lines.append("")

	text = "\n".join(lines)
	if write_file:
		frappe.get_writer(write_file, overwrite=True).write(text)  # type: ignore[attr-defined]
	return text
