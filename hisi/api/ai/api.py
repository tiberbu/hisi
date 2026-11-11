# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""Whitelisted AI endpoints — the self-contained AI surface.

- ``list_discrepancy_tags``       the tag catalog (the discrepancy vocabulary).
- ``generate_discrepancy_report`` create the report job (``Queued``) and
  enqueue the async worker; returns 202 ``{report, status}`` for polling.
- ``get_discrepancy_report``      one job's current state + result.
- ``list_discrepancy_reports``    recent jobs (optionally per CR ID/status).
- ``chat``                        one synchronous chat completion (optionally
  grounded in a member's data context).

The report is async because a multi-minute LLM run must not hold the app
proxy's 30 s upstream window; ``chat`` is a short call and stays sync.
Failures land in the standard error envelope via ``@api_endpoint``; the LLM
is fail-closed — an unconfigured host never returns a success payload.
"""

import json

import frappe

from hisi.api.ai import llm_client
from hisi.api.ai.benefits_registry import matched_benefits_lines
from hisi.api.ai.prompt import DEFAULT_SYSTEM_PROMPT, build_data_context, effective_benefits_reference
from hisi.api.ai.seed import ensure_default_tags
from hisi.api.ai.settings import get_ai_settings
from hisi.api.patient import (
	fetch_patient_contributions,
	step_claims,
	step_consent,
	step_preauth,
)
from hisi.api.response_handler import APIResponse, BadRequest, NotFound, UpstreamError, api_endpoint

REPORT = "HISI Discrepancy Report"
TAG = "HISI Discrepancy Tag"

#: Chat grounding is bounded — the proxy's 30 s window — so chat fetches the
#: quick sources only (never the FHIR ``records`` step) with a small cap.
_CHAT_SOURCES = ("consent", "contributions", "claims", "preauth")
_CHAT_MAX_RECORDS = 25
_CHAT_TOTAL_TIMEOUT_CAP = 120


def _parse_body(data) -> dict:
	"""The shared body parser: a ``data`` kwarg (JSON string or dict)."""
	if isinstance(data, dict):
		return data
	if isinstance(data, str):
		raw = data.strip()
		if not raw:
			raise BadRequest("A JSON object body is required.")
		try:
			parsed = json.loads(raw)
		except (json.JSONDecodeError, ValueError):
			raise BadRequest("The body must be a JSON object.")
		if not isinstance(parsed, dict):
			raise BadRequest("The body must be a JSON object.")
		return parsed
	raise BadRequest("The body must be a JSON object.")


@frappe.whitelist(methods=["GET", "POST"])
@api_endpoint
def list_discrepancy_tags() -> None:
	"""The tag catalog: the discrepancy checks available to the report."""
	ensure_default_tags()
	rows = frappe.get_all(TAG, fields=["name", "tag_code", "label", "severity", "enabled", "instruction"])
	rows.sort(key=lambda row: row["tag_code"])
	return APIResponse.success(data={"tags": rows})


@frappe.whitelist(methods=["POST"])
@api_endpoint
def generate_discrepancy_report(data: str | dict = "") -> None:
	"""Create one discrepancy-report job for a CR ID and enqueue the worker.

	Body: ``{"cr_id": "...", "tag_codes": ["..."]?}`` — ``tag_codes``
	(optionally) restricts the run to a subset of enabled tags. Returns 202
	with the job name; poll ``get_discrepancy_report`` for the result.
	"""
	body = _parse_body(data)
	cr_id = str(body.get("cr_id") or "").strip()
	if not cr_id:
		raise BadRequest("cr_id is required.")
	tag_codes = body.get("tag_codes")
	if tag_codes is not None:
		if not isinstance(tag_codes, list) or not all(isinstance(code, str) for code in tag_codes):
			raise BadRequest("tag_codes must be a list of tag codes.")
		tag_codes = [code.strip() for code in tag_codes if code.strip()]
	ensure_default_tags()
	filters = {"enabled": 1}
	if tag_codes:
		filters["tag_code"] = ["in", tag_codes]
	tags = frappe.get_all(TAG, filters=filters, fields=["tag_code", "label", "instruction", "severity"])
	if not tags:
		raise BadRequest("No enabled discrepancy tags to run (enable tags or set tag_codes).")
	snapshot = [
		{"tag_code": t.tag_code, "label": t.label, "instruction": t.instruction, "severity": t.severity}
		for t in tags
	]
	doc = frappe.get_doc(
		{
			"doctype": REPORT,
			"naming_series": "HIS-DRP-.YYYY.-.####",
			"subject_cr_id": cr_id,
			"status": "Queued",
			"tag_snapshot": json.dumps(snapshot, ensure_ascii=False),
		}
	)
	doc.insert()
	settings = get_ai_settings()
	frappe.enqueue(
		"hisi.api.ai.worker.run_discrepancy_report",
		queue="long",
		timeout=int(settings.llm_total_timeout_sec) + 300,
		job_id=doc.name,
		report_name=doc.name,
	)
	frappe.db.commit()
	return APIResponse.success(data={"report": doc.name, "status": "queued"}, status=202)


@frappe.whitelist(methods=["GET", "POST"])
@api_endpoint
def get_discrepancy_report(name: str) -> None:
	"""One report job's current state; on Success the markdown report +
	normalized findings; on Failed the error object."""
	name = str(name or "").strip()
	if not name:
		raise BadRequest("name is required.")
	try:
		doc = frappe.get_doc(REPORT, name)
	except frappe.DoesNotExistError:
		raise NotFound(f"Discrepancy report {name!r} not found.")
	payload = {
		"name": doc.name,
		"subject_cr_id": doc.subject_cr_id,
		"subject_name": doc.subject_name,
		"status": doc.status,
		"data_sources": doc.data_sources,
		"model": doc.model,
		"creation": str(doc.creation) if doc.creation else None,
		"started_at": str(doc.started_at) if doc.started_at else None,
		"finished_at": str(doc.finished_at) if doc.finished_at else None,
		"duration_ms": doc.duration_ms,
	}
	if doc.status == "Success":
		payload["report"] = doc.report
		try:
			payload["findings"] = json.loads(doc.findings) if doc.findings else None
		except (json.JSONDecodeError, ValueError):
			payload["findings"] = None
	elif doc.status == "Failed":
		try:
			payload["error"] = json.loads(doc.error) if doc.error else {"code": "UNKNOWN", "message": ""}
		except (json.JSONDecodeError, ValueError):
			payload["error"] = {"code": "UNKNOWN", "message": str(doc.error or "")}
	return APIResponse.success(data=payload)

@frappe.whitelist(methods=["GET", "POST"])
@api_endpoint
def list_discrepancy_reports(cr_id=None, status=None, page=None, page_size=None) -> None:
	"""Recent report jobs, paged; optional cr_id / status filters."""
	filters = {}
	if cr_id:
		filters["subject_cr_id"] = str(cr_id).strip()
	if status:
		status = str(status).strip()
		if status not in ("Queued", "Processing", "Success", "Failed"):
			raise BadRequest("Unknown status filter.")
		filters["status"] = status
	try:
		page = max(1, int(page or 1))
	except (TypeError, ValueError):
		page = 1
	try:
		page_size = int(page_size or 10)
	except (TypeError, ValueError):
		page_size = 10
	page_size = min(max(page_size, 1), 100)
	rows = frappe.get_all(
		REPORT,
		filters=filters,
		fields=["name", "subject_cr_id", "subject_name", "status", "data_sources", "model", "creation", "started_at", "finished_at", "duration_ms"],
		order_by="creation desc",
		limit_start=(page - 1) * page_size,
		limit=page_size,
	)
	total = frappe.db.count(REPORT, filters)
	return APIResponse.success(
		data={
			"reports": [row.as_dict() for row in rows],
			"total": total,
			"page": page,
			"page_size": page_size,
		}
	)
@frappe.whitelist(methods=["POST"])
@api_endpoint
def chat(data: str | dict = "") -> None:
	"""One synchronous chat completion.

	Body: ``{"message": "...", "cr_id": "..."?}`` — with ``cr_id`` the reply
	is grounded in the member's quick data context (the FHIR records step is
	never included, to stay inside the 30 s proxy window).
	"""
	body = _parse_body(data)
	message = str(body.get("message") or "").strip()
	if not message:
		raise BadRequest("message is required.")
	cr_id = str(body.get("cr_id") or "").strip()
	settings = get_ai_settings()
	if not settings.llm_base_url:
		raise UpstreamError("llm_base_url is not configured in HISI AI Settings.")
	system = str(settings.discrepancy_system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT
	benefits = effective_benefits_reference(settings.benefits_reference)
	if benefits:
		system = f"{system}\n\nBenefits Reference:\n{benefits}"
	messages = [{"role": "system", "content": system}]
	if cr_id:
		results, errors = _quick_context(cr_id)
		context = build_data_context(cr_id, list(_CHAT_SOURCES), results, errors, _CHAT_MAX_RECORDS)
		payload = json.dumps(context, indent=2, ensure_ascii=False, default=str)
		detail = matched_benefits_lines(payload)
		if detail:
			system = f"{system}\n\nBenefits Detail (registry rows for codes present in the member data):\n{detail}"
		messages.append({"role": "user", "content": "Member data (ground the answer in this data only):\n" + payload})
	messages.append({"role": "user", "content": message})
	raw, usage = llm_client.stream_chat_completion(
		base_url=settings.llm_base_url,
		api_key=settings.llm_api_key or "",
		model=str(settings.llm_model or "qwen3-27b"),
		messages=messages,
		idle_timeout_sec=int(settings.llm_idle_timeout_sec),
		total_timeout_sec=min(int(settings.llm_total_timeout_sec), _CHAT_TOTAL_TIMEOUT_CAP),
	)
	reply = (raw or "").strip()
	if not reply:
		raise UpstreamError("The LLM returned an empty reply.")
	return APIResponse.success(data={"reply": reply, "usage": usage, "model": str(settings.llm_model or "")})


def _quick_context(cr_id: str) -> tuple:
	"""The bounded chat grounding: the quick sources, each error-isolated."""
	results: dict = {}
	errors: dict = {}
	for step in _CHAT_SOURCES:
		try:
			if step == "consent":
				results[step] = step_consent(cr_id)
			elif step == "contributions":
				results[step] = fetch_patient_contributions(cr_id)
			elif step == "claims":
				results[step] = step_claims(cr_id)
			elif step == "preauth":
				results[step] = step_preauth(cr_id)
		except (BadRequest, NotFound, UpstreamError) as exc:
			errors[step] = exc.message or str(exc)
		except Exception as exc:
			errors[step] = str(exc)
	return results, errors
