# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""One-run setup check + patch for the AI discrepancy / benefits stack.

Run with::

	bench --site hisi-local execute hisi.api.ai.setup_check.run_setup_check

Audits every configurable piece and sorts the result into three buckets:

- ``ready``        — already configured (incl. informational current values).
- ``auto_fixed``   — applied safely by this run (idempotent: tag seeding,
                     registry load on an empty table).
- ``action_needed``— manual configuration still required (LLM credentials,
                     missing doctypes, missing xlsx).

Safe to re-run; nothing destructive. The LLM API key is never echoed —
only its presence is reported.
"""

import frappe

from hisi.api.ai.benefits_loader import _DEFAULT_DOCS, _DEFAULT_F1, _DEFAULT_F2, load_benefits_registry
from hisi.api.ai.benefits_registry import REGISTRY
from hisi.api.ai.seed import CLAIM_STATUS_CONFIG, ensure_claim_status_config, ensure_default_tags
from hisi.api.ai.settings import get_ai_settings

REPORT = "HISI Discrepancy Report"
TAG = "HISI Discrepancy Tag"
SWEEP_HOOK = "hisi.api.ai.worker.sweep_stuck_reports"


def run_setup_check(apply: bool = True) -> dict:
	ready: list[str] = []
	auto_fixed: list[str] = []
	action_needed: list[str] = []

	# 1. Doctypes present (all four must exist for the stack to run).
	for dt in (REPORT, TAG, REGISTRY, CLAIM_STATUS_CONFIG):
		if frappe.db.exists("DocType", dt):
			ready.append(f"doctype present: {dt}")
		else:
			action_needed.append(f"doctype missing: {dt} — run `bench --site <site> migrate`")

	# 2. Benefits registry populated (auto-fix: load from the bundled xlsx).
	if frappe.db.exists("DocType", REGISTRY):
		rows = frappe.db.count(REGISTRY)
		if rows:
			ready.append(f"benefits registry: {rows} rows loaded")
		elif apply:
			if all(p.exists() for p in (_DEFAULT_F1, _DEFAULT_F2, _DEFAULT_DOCS)):
				summary = load_benefits_registry()
				auto_fixed.append(f"benefits registry loaded: {summary['total']} rows (created {summary['created']})")
			else:
				missing = ", ".join(str(p.name) for p in (_DEFAULT_F1, _DEFAULT_F2, _DEFAULT_DOCS) if not p.exists())
				action_needed.append(
					f"benefits registry empty and matrix file(s) missing: {missing} — "
					"place the 3 xlsx next to hisi/api/ai/ and re-run"
				)
		else:
			action_needed.append("benefits registry empty — re-run with apply=True (or load_benefits_registry directly)")

	# 3. Default discrepancy tags seeded (idempotent).
	if frappe.db.exists("DocType", TAG):
		if apply:
			ensure_default_tags()
		tag_count = frappe.db.count(TAG)
		if tag_count:
			enabled = frappe.db.count(TAG, {"enabled": 1})
			ready.append(f"discrepancy tags: {tag_count} total, {enabled} enabled")
		else:
			action_needed.append("no discrepancy tags — none seeded (check hisi.api.ai.seed)")

	# 3b. Claim-status config seeded (idempotent) — maps raw claim_status values
	#     to whether the claim reached the payer, so reports can flag claims that
	#     were not successfully dispatched.
	if frappe.db.exists("DocType", CLAIM_STATUS_CONFIG):
		if apply:
			ensure_claim_status_config()
		status_count = frappe.db.count(CLAIM_STATUS_CONFIG)
		if status_count:
			ready.append(f"claim-status config: {status_count} status(es) mapped")
		else:
			action_needed.append("claim-status config empty — none seeded (check hisi.api.ai.seed)")

	# 4. Scheduler hook for the stuck-report sweep.
	# This fork's get_hooks returns one merged {event: [...]} dict (upstream
	# Frappe returns [{app: {event: [...]}}]) — handle either shape.
	hooks = frappe.get_hooks("scheduler_events")
	daily: list = []
	if isinstance(hooks, dict):
		daily = hooks.get("daily") or []
	elif isinstance(hooks, list):
		for entry in hooks:
			if isinstance(entry, dict):
				for app_hooks in entry.values():
					if isinstance(app_hooks, dict):
						daily.extend(app_hooks.get("daily") or [])
	sweep_registered = SWEEP_HOOK in daily
	if sweep_registered:
		ready.append("scheduler: daily sweep_stuck_reports registered")
	else:
		action_needed.append("scheduler: sweep_stuck_reports not in hooks.scheduler_events.daily")

	# 5. HISI AI Settings — the operator-configured surface.
	if frappe.db.exists("DocType", "HISI AI Settings"):
		settings = get_ai_settings()
		if str(settings.llm_base_url or "").strip():
			ready.append(f"llm_base_url: configured ({settings.llm_base_url})")
		else:
			action_needed.append("llm_base_url: NOT set — HISI AI Settings → LLM Base URL (OpenAI-compatible /v1)")
		if str(settings.llm_api_key or "").strip():
			ready.append("llm_api_key: configured (value masked)")
		else:
			action_needed.append("llm_api_key: NOT set — HISI AI Settings → LLM API Key")
		ready.append(f"llm_model: {settings.llm_model or '(default qwen3-27b)'}")
		ready.append(f"timeouts: idle {settings.llm_idle_timeout_sec}s / total {settings.llm_total_timeout_sec}s")
		if str(settings.benefits_reference or "").strip():
			ready.append("benefits_reference: operator override active (built-in digest bypassed)")
		else:
			ready.append("benefits_reference: built-in SHA digest in use (no override)")
		ready.append(f"data sources: {settings.discrepancy_data_sources or '(default)'} · max records {settings.discrepancy_max_records or 100}")
	else:
		action_needed.append("HISI AI Settings doctype missing — run `bench --site <site> migrate`")

	# 6. Matrix xlsx availability (informational — runtime does not need them).
	present = [p.name for p in (_DEFAULT_F1, _DEFAULT_F2, _DEFAULT_DOCS) if p.exists()]
	if present:
		ready.append(f"matrix xlsx present for re-loads: {len(present)}/3")
	else:
		ready.append("matrix xlsx absent (runtime unaffected; registry re-load would need the files restored)")

	return {"ready": ready, "auto_fixed": auto_fixed, "action_needed": action_needed}
