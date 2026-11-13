# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""The optional AI stages — narrow, routed, redacted (the v2 dial).

The rule engine produces the findings; this module is what the model is
allowed to add on top: a validated executive-summary narrative (and, in
later phases, document reading / investigation). Guarantees:

- the model sees the REDACTED digest only (the full context stays local);
- the model's job is a strict JSON contract with length caps — it never
  produces or edits findings;
- pre-flight budget check (estimate vs ``max_input_tokens``) — over budget
  the stage is skipped with a stored warning and the deterministic summary
  stands;
- one corrective retry on a malformed reply (``max_llm_calls`` bounds the
  stage); a PII scan rejects a reply that leaks a phone/DOB/CR shape;
- model routing: stage -> model via ``HISI Report Settings``
  (``stage_model_routing`` then the class default).

The report is never blocked on the model: every failure path returns the
deterministic fallback with a warning.
"""

import json
import re

from hisi.api.ai import llm_client
from hisi.api.ai.report_settings import stage_model

#: Stage name for the narrative call (routing table key).
NARRATIVE_STAGE = "narrative"

#: Hard caps on the model's prose (the contract pins them).
MAX_SUMMARY_CHARS = 400
MAX_EXECUTIVE_CHARS = 1200

NARRATIVE_CONTRACT = (
	"You are given (a) a short digest of a member's claims data and (b) the "
	"findings of a deterministic discrepancy analysis that has ALREADY been "
	"computed and evidence-verified. Write ONLY the human-readable narrative. "
	"Do NOT invent, add, remove, re-grade or re-verify any finding — the "
	"findings are final. Respond with ONLY a JSON object, no markdown, no "
	"commentary, in exactly this shape: "
	'{"summary": string, "executive_summary": string}\n'
	"`summary` is one or two sentences stating the overall result. "
	"`executive_summary` is a management-level overview of three to six "
	"sentences: what was audited, the most significant discrepancies and "
	"their likely financial or compliance impact, and what should happen "
	"next — written for a reviewer who will not read the line items. "
	"Use the exact figures and identifiers from the input; never invent "
	"records, dates, amounts or identifiers."
)

_CORRECTIVE = "Your last reply was not valid JSON. Reply with the JSON object only."

#: PII shapes that must not survive the PII scan (a hit rejects the reply).
_PII_PATTERNS = (
	re.compile(r"\+?\d{9,}"),  # phone-shaped
	re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # full date-shaped (a DOB leak)
)


def pii_scan(text: str, allowed: str = "") -> list:
	"""PII shapes in a model reply that are NOT present in the allowed input
	text (the prompt's data + findings). A reply may restate the service
	dates and amounts it was given; a value that appears nowhere in the
	input is a leak (rejected). Empty list = clean."""
	if not text:
		return []
	allowed = str(allowed or "")
	hits = []
	for pattern in _PII_PATTERNS:
		for match in pattern.findall(str(text)):
			value = str(match)
			if value in allowed:
				continue
			hits.append(value)
	return hits


def _estimate_tokens(text: str) -> int:
	"""Rough token estimate (chars/4) for the pre-flight budget check."""
	return max(1, len(str(text or "")) // 4)


def _validate_narrative(obj: dict, allowed_text: str = "") -> dict | None:
	"""Shape/validate the model's reply against the narrative contract."""
	if not isinstance(obj, dict):
		return None
	summary = str(obj.get("summary") or "").strip()
	executive = str(obj.get("executive_summary") or "").strip()
	if not summary or not executive:
		return None
	if len(summary) > MAX_SUMMARY_CHARS or len(executive) > MAX_EXECUTIVE_CHARS:
		return None
	if pii_scan(summary + " " + executive, allowed_text):
		return None  # a PII-shaped leak → reject (deterministic summary stands)
	return {
		"summary": summary,
		"executive_summary": executive,
	}


def build_narrative_prompt(cr_id: str, digest: dict, result: dict) -> str:
	"""The narrative prompt: digest + findings + contract (one user turn)."""
	payload = {
		"cr_id": cr_id,
		"digest": digest,
		"result": {
			"overall": result.get("overall"),
			"findings": [
				{
					"tag_code": f.get("tag_code"),
					"severity": f.get("severity"),
					"title": f.get("title"),
					"detail": f.get("detail"),
					"evidence": (f.get("evidence") or [])[:5],
				}
				for f in (result.get("findings") or [])[:50]
			],
		},
	}
	return (
		"Member data digest (redacted):\n"
		+ json.dumps(digest, indent=2, ensure_ascii=False, default=str)
		+ "\n\nDeterministic analysis result:\n"
		+ json.dumps(payload["result"], indent=2, ensure_ascii=False, default=str)
		 + "\n\n"
		+ NARRATIVE_CONTRACT
	)


def run_narrative_stage(
	report_settings,
	ai_settings,
	cr_id: str,
	redacted_context: dict,
	result: dict,
	mode: str = "",
) -> dict:
	"""The optional narrative stage. Returns
	``{"result": dict|None, "model": str|None, "usage": dict|None,
	"warnings": list}`` — ``result`` is ``None`` when the stage was skipped
	or failed (the caller keeps the deterministic summary).

	``mode`` is the resolved mode of the run (the site default is only a
	fallback for direct callers)."""
	warnings: list = []
	mode = str(mode or report_settings.get("default_ai_mode") or "")
	# The stage runs for the AI modes that use narrative (the worker already
	# dispatches; this guard keeps the module safe to call directly).
	if mode in ("deterministic_only", "legacy_llm"):
		return {"result": None, "model": None, "usage": None, "warnings": ["narrative stage not used in mode " + mode]}

	model = stage_model(NARRATIVE_STAGE, report_settings)
	if not str(ai_settings.get("llm_base_url") or "").strip():
		return {
			"result": None,
			"model": None,
			"usage": None,
			"warnings": ["narrative stage skipped: llm_base_url not configured (deterministic summary used)"],
		}
	prompt = build_narrative_prompt(cr_id, redacted_context, result)
	max_input_tokens = int(report_settings.get("max_input_tokens") or 24000)
	if _estimate_tokens(prompt) > max_input_tokens:
		return {
			"result": None,
			"model": None,
			"usage": None,
			"warnings": [
				f"narrative stage skipped: estimated input ~{_estimate_tokens(prompt)} tokens exceeds max_input_tokens {max_input_tokens} "
				"(deterministic summary used)"
			],
		}
	max_calls = max(1, int(report_settings.get("max_llm_calls") or 2))
	messages = [{"role": "user", "content": prompt}]
	usage = None
	for attempt in range(max_calls):
		try:
			raw, call_usage = llm_client.stream_chat_completion(
				base_url=str(ai_settings.get("llm_base_url")),
				api_key=str(ai_settings.get("llm_api_key") or ""),
				model=model,
				messages=messages,
				idle_timeout_sec=int(ai_settings.get("llm_idle_timeout_sec")),
				total_timeout_sec=int(ai_settings.get("llm_total_timeout_sec")),
			)
		except llm_client.LLMError as exc:
			return {
				"result": None,
				"model": model,
				"usage": usage,
				"warnings": [f"narrative stage failed ({type(exc).__name__}: {exc}); deterministic summary used"],
			}
		usage = call_usage if call_usage else usage
		parsed = None
		try:
			parsed = json.loads(raw) if raw and raw.strip().startswith("{") else None
		except (json.JSONDecodeError, ValueError):
			parsed = None
		if parsed is None:
			from hisi.api.ai.prompt import extract_json_object

			parsed = extract_json_object(raw)
		validated = _validate_narrative(parsed, prompt) if parsed is not None else None
		if validated is not None:
			return {"result": validated, "model": model, "usage": usage, "warnings": warnings}
		# One corrective retry (the raw reply echoed as an assistant turn).
		messages = messages + [
			{"role": "assistant", "content": raw or ""},
			{"role": "user", "content": _CORRECTIVE},
		]
	return {
		"result": None,
		"model": model,
		"usage": usage,
		"warnings": ["narrative stage produced no valid contract JSON after retry; deterministic summary used"],
	}
