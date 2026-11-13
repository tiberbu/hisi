# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""Whitelisted AI endpoints — the self-contained AI surface.

- ``list_discrepancy_tags``       the tag catalog (the discrepancy vocabulary).
- ``generate_discrepancy_report`` create the report job (``Queued``) and
  enqueue the async worker; returns 202 ``{report, status}`` for polling.
- ``retry_discrepancy_report``    re-run a Failed job (reset to ``Queued``,
  error cleared) or re-enqueue a stuck ``Queued`` one; 409 otherwise.
- ``get_discrepancy_report``      one job's current state + result.
- ``list_discrepancy_reports``    recent jobs (optionally per CR ID/status).
- ``chat``                        one synchronous chat completion (optionally
  grounded in a member's data context).

The report is async because a multi-minute LLM run must not hold the app
proxy's 30 s upstream window; ``chat`` is a short call and stays sync.
The completed report's deliverable is a PDF (a private File attached to the
job; ``get_discrepancy_report_pdf`` serves it as base64 for the preview).
``generate_discrepancy_report`` is per-patient deduped: a patient that
already has a report gets it back (no new LLM spend) unless ``force`` is
set. Failures land in the standard error envelope via ``@api_endpoint``;
the LLM is fail-closed — an unconfigured host never returns a success
payload.
"""

import base64
import json

import frappe

from hisi.api.ai import llm_client, report_pdf
from hisi.api.ai.benefits_registry import matched_benefits_lines
from hisi.api.ai.prompt import DEFAULT_SYSTEM_PROMPT, build_data_context, effective_benefits_reference
from hisi.api.ai.rules.engine import AI_MODES  # noqa: F401  (re-exported for API consumers)
from hisi.api.ai.seed import ensure_claim_status_config, ensure_default_tags
from hisi.api.ai.settings import get_ai_settings
from hisi.api.patient import (
	fetch_patient_contributions,
	step_claims,
	step_consent,
	step_preauth,
)
from hisi.api.response_handler import APIResponse, BadRequest, Conflict, NotFound, UpstreamError, api_endpoint

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

	Body: ``{"cr_id": "...", "tag_codes": ["..."]?, "force": bool?}`` —
	``tag_codes`` (optionally) restricts the run to a subset of enabled
	tags; ``subject_claim`` (optionally) scopes the report to a single
	claim (a payment-decision report — the claim's record name or bundle id).
	Per-patient dedupe: unless ``force`` is true, the member's
	newest non-failed report (running or completed) is returned as-is, so
	re-searching an already-audited patient spends no new LLM run. A fresh
	run returns 202 with the job name; a cached report returns 200 with
	``existing: true``. Poll ``get_discrepancy_report`` for the result.
	"""
	body = _parse_body(data)
	cr_id = str(body.get("cr_id") or "").strip()
	if not cr_id:
		raise BadRequest("cr_id is required.")
	force = bool(body.get("force"))
	ai_mode = str(body.get("ai_mode") or "").strip().lower()
	if ai_mode and ai_mode not in AI_MODES:
		raise BadRequest(f"ai_mode must be one of: {', '.join(AI_MODES)}.")
	tag_codes = body.get("tag_codes")
	if tag_codes is not None:
		if not isinstance(tag_codes, list) or not all(isinstance(code, str) for code in tag_codes):
			raise BadRequest("tag_codes must be a list of tag codes.")
		tag_codes = [code.strip() for code in tag_codes if code.strip()]
	subject_claim = str(body.get("subject_claim") or "").strip()
	subject_visit = str(body.get("subject_visit") or "").strip()
	subject_preauth = str(body.get("subject_preauth") or "").strip()
	model_pref = str(body.get("model") or body.get("model_pref") or "auto").strip().lower()
	if model_pref not in ("", "auto", "clinical", "general", "both"):
		raise BadRequest("model must be one of: auto, clinical, general, both.")
	if sum(bool(x) for x in (subject_claim, subject_visit, subject_preauth)) > 1:
		raise BadRequest("subject_claim, subject_visit and subject_preauth are mutually exclusive.")
	if not force:
		# Per-patient dedupe: the newest live or completed job for this
		# member with the SAME scope is the answer — hand it back without
		# spending a new LLM run (the surface can pass force=true for a
		# fresh analysis). A different scope is a different report.
		candidates = frappe.get_all(
			REPORT,
			filters={"subject_cr_id": cr_id, "status": ["in", ["Queued", "Processing", "Success"]]},
			fields=["name", "status", "subject_claim", "subject_visit", "subject_preauth"],
			order_by="creation desc",
			limit=5,
		)
		scope = (subject_claim, subject_visit, subject_preauth)
		for row in candidates:
			if (
				str(row.get("subject_claim") or "").strip(),
				str(row.get("subject_visit") or "").strip(),
				str(row.get("subject_preauth") or "").strip(),
			) == scope:
				return APIResponse.success(data={"report": row.name, "status": row.status, "existing": True}, status=200)
	ensure_default_tags()
	ensure_claim_status_config()
	filters = {"enabled": 1}
	if tag_codes:
		filters["tag_code"] = ["in", tag_codes]
	tags = frappe.get_all(TAG, filters=filters, fields=["tag_code", "label", "instruction", "severity", "rule_type", "rule_key", "parameters", "severity_policy", "version"])
	if not tags:
		raise BadRequest("No enabled discrepancy tags to run (enable tags or set tag_codes).")
	snapshot = [
		{
			"tag_code": t.tag_code,
			"label": t.label,
			"instruction": t.instruction,
			"severity": t.severity,
			"rule_type": t.rule_type or "code",
			"rule_key": t.rule_key or "",
			"parameters": t.parameters or "",
			"severity_policy": t.severity_policy or "fixed",
			"version": t.version,
		}
		for t in tags
	]
	doc = frappe.get_doc(
		{
			"doctype": REPORT,
			"naming_series": "HIS-DRP-.YYYY.-.####",
			"subject_cr_id": cr_id,
			"subject_claim": subject_claim or None,
			"subject_visit": subject_visit or None,
			"subject_preauth": subject_preauth or None,
			"model_pref": model_pref or "auto",
			"status": "Queued",
			"tag_snapshot": json.dumps(snapshot, ensure_ascii=False),
			"ai_mode": ai_mode or None,
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


@frappe.whitelist(methods=["POST"])
@api_endpoint
def retry_discrepancy_report(name: str) -> None:
	"""Re-run an existing job in place (no new job, no new tag selection).

	A ``Failed`` job is reset to ``Queued`` (one guarded UPDATE — the error
	and finished_at are cleared) and the worker is enqueued again; a stuck
	``Queued`` job is simply re-enqueued (the worker's atomic claim keeps a
	double enqueue harmless: the loser skips with "not claimable"). A
	``Processing`` job is refused 409 — it may still be live (the daily
	sweep marks crashed runs failed, after which a retry is possible); a
	``Success`` job 409s too — generate with ``force`` for a fresh run.
	"""
	name = str(name or "").strip()
	if not name:
		raise BadRequest("name is required.")
	try:
		doc = frappe.get_doc(REPORT, name)
	except frappe.DoesNotExistError:
		raise NotFound(f"Discrepancy report {name!r} not found.")
	if doc.status == "Success":
		raise Conflict(f"{name!r} already completed — generate with force to run it again.")
	if doc.status == "Processing":
		raise Conflict(f"{name!r} is still running (a crashed run is marked failed by the daily sweep, then retryable).")
	if doc.status == "Failed":
		affected = frappe.db.execute_query(
			"UPDATE `tabHISI Discrepancy Report` SET status = %s, error = NULL, finished_at = NULL WHERE name = %s AND status = %s",
			("Queued", name, "Failed"),
		)
		if not affected:
			raise Conflict(f"{name!r} changed state before the retry; reload and try again.")
	settings = get_ai_settings()
	frappe.enqueue(
		"hisi.api.ai.worker.run_discrepancy_report",
		queue="long",
		timeout=int(settings.llm_total_timeout_sec) + 300,
		job_id=name,
		report_name=name,
	)
	if not frappe.flags.in_test:
		frappe.db.commit()
	return APIResponse.success(data={"report": name, "status": "queued"}, status=202)


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
		"subject_claim": doc.subject_claim,
		"subject_visit": doc.subject_visit,
		"subject_preauth": doc.subject_preauth,
		"status": doc.status,
		"data_sources": doc.data_sources,
		"ai_mode_used": doc.ai_mode_used,
		"catalog_version": doc.catalog_version,
		"benefits_source": doc.benefits_source,
		"models_used": doc.models_used,
		"data_context_hash": doc.data_context_hash,
		"model": doc.model,
		"creation": str(doc.creation) if doc.creation else None,
		"started_at": str(doc.started_at) if doc.started_at else None,
		"finished_at": str(doc.finished_at) if doc.finished_at else None,
		"duration_ms": doc.duration_ms,
		"data_as_of": str(doc.data_as_of) if doc.data_as_of else None,
		"coverage": json.loads(doc.coverage) if doc.coverage else None,
	}
	if doc.status == "Success":
		payload["report"] = doc.report
		try:
			payload["findings"] = json.loads(doc.findings) if doc.findings else None
		except (json.JSONDecodeError, ValueError):
			payload["findings"] = None
		if doc.findings_provenance:
			try:
				provenance = json.loads(doc.findings_provenance)
				# The audit blob: {mode, catalog_version, benefits_source, warnings, findings}
				# — expose the per-finding provenance list (the audit extras stay on the doc).
				payload["findings_provenance"] = provenance.get("findings") if isinstance(provenance, dict) else provenance
				if isinstance(provenance, dict) and provenance.get("warnings"):
					payload["warnings"] = provenance["warnings"]
			except (json.JSONDecodeError, ValueError):
				payload["findings_provenance"] = None
		payload["pdf"] = _attached_report_pdf(doc.name)
	elif doc.status == "Failed":
		try:
			payload["error"] = json.loads(doc.error) if doc.error else {"code": "UNKNOWN", "message": ""}
		except (json.JSONDecodeError, ValueError):
			payload["error"] = {"code": "UNKNOWN", "message": str(doc.error or "")}
	return APIResponse.success(data=payload)

def _attached_report_pdf(report_name: str) -> dict | None:
	"""The run's PDF deliverable (the private File the worker attached), or
	None when the run produced no PDF file (e.g. a host without the engine)."""
	rows = frappe.get_all(
		"File",
		filters={"attached_to_doctype": REPORT, "attached_to_name": report_name},
		fields=["name", "file_name", "file_type", "file_size", "file_url"],
	)
	for row in rows:
		if str(row.file_name or "").lower().endswith(".pdf"):
			return {
				"name": row.name,
				"file_name": row.file_name,
				"file_type": row.file_type,
				"file_size": row.file_size,
				"file_url": row.file_url,
			}
	return None

def _render_and_attach_pdf(doc) -> dict | None:
	"""Lazy render for a completed report without a PDF file — the runs that
	predate the PDF deliverable (or one whose best-effort render failed).
	Rebuilds the PDF from the stored normalized findings and attaches it, so
	any completed report can be previewed/downloaded on demand; the file
	stays for subsequent reads."""
	try:
		findings = json.loads(doc.findings) if doc.findings else None
	except (json.JSONDecodeError, ValueError):
		findings = None
	if not isinstance(findings, dict):
		return None
	try:
		pdf_bytes = report_pdf.build_report_pdf_bytes(
			str(doc.subject_cr_id or ""),
			str(doc.subject_name or ""),
			findings,
			duration_ms=doc.duration_ms,
			data_sources=doc.data_sources,
			report_ref=doc.name,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"lazy report pdf render {doc.name}")
		return None
	try:
		report_pdf.attach_report_pdf(doc.name, pdf_bytes)
		if not frappe.flags.in_test:
			frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"lazy report pdf attach {doc.name}")
		return None
	return _attached_report_pdf(doc.name)

@frappe.whitelist(methods=["GET", "POST"])
@api_endpoint
def get_discrepancy_report_pdf(name: str) -> None:
	"""The report's PDF deliverable as base64 (the preview/download feed).

	A completed report without an attached file (a run that predates the
	PDF deliverable) is rendered on demand from its stored findings, so
	every completed report can be previewed and downloaded."""
	name = str(name or "").strip()
	if not name:
		raise BadRequest("name is required.")
	try:
		doc = frappe.get_doc(REPORT, name)
	except frappe.DoesNotExistError:
		raise NotFound(f"Discrepancy report {name!r} not found.")
	attached = _attached_report_pdf(doc.name)
	if not attached and doc.status == "Success":
		attached = _render_and_attach_pdf(doc)
	if not attached:
		raise NotFound("No PDF for this report (a PDF could not be rendered for it).")
	file_doc = frappe.get_doc("File", attached["name"])
	content = file_doc.get_content()
	raw = content if isinstance(content, bytes) else str(content).encode("utf-8")
	# This fork's File.file_type stores the uppercase extension ("PDF"), not
	# a mime type — normalize it, or the preview blob renders as raw bytes.
	mime = str(attached["file_type"] or "").strip()
	if mime.upper() == "PDF":
		mime = "application/pdf"
	return APIResponse.success(
		data={
			"file_name": attached["file_name"],
			"mime_type": mime or "application/pdf",
			"file_size": attached["file_size"],
			"content_b64": base64.b64encode(raw).decode("ascii"),
		}
	)

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
		fields=["name", "subject_cr_id", "subject_name", "status", "data_sources", "model", "creation", "started_at", "finished_at", "duration_ms", "data_as_of"],
		order_by="creation desc",
		limit_start=(page - 1) * page_size,
		limit=page_size,
	)
	total = frappe.db.count(REPORT, filters)
	return APIResponse.success(
		data={
			# get_all hands back plain dict rows (no as_dict on them).
			"reports": [dict(row) for row in rows],
			"total": total,
			"page": page,
			"page_size": page_size,
		}
	)


@frappe.whitelist(methods=["POST"])
@api_endpoint
def set_finding_verdict(data: str | dict = "") -> None:
	"""M13 analyst override: set a per-finding verdict on a successful report.

	Body: ``{name, finding_index, verdict}`` with verdict in
	``valid | false_positive | unknown``. The verdict is written back into the
	stored findings payload (the report's contract is the findings JSON — no
	schema change needed) with an audit trail: ``verdict_updated_by`` (the
	session user, or the ``updated_by`` override for service callers) and
	``verdict_updated_at``. It feeds the tuning report (verdict distribution
	per rule) and the UI's per-finding verdict chip.
	"""
	payload = _parse_body(data)
	name = str(payload.get("name") or "").strip()
	if not name:
		raise BadRequest("name is required.")
	try:
		finding_index = int(payload.get("finding_index"))
	except (TypeError, ValueError):
		raise BadRequest("finding_index is required (integer).")
	verdict = str(payload.get("verdict") or "unknown").strip().lower()
	if verdict not in ("valid", "false_positive", "unknown"):
		raise BadRequest("verdict must be one of: valid, false_positive, unknown.")
	try:
		doc = frappe.get_doc(REPORT, name)
	except frappe.DoesNotExistError:
		raise NotFound(f"Discrepancy report {name!r} not found.")
	if doc.status != "Success":
		raise Conflict(f"Verdicts can only be set on successful reports (status: {doc.status}).")
	try:
		parsed = json.loads(doc.findings) if doc.findings else None
	except (json.JSONDecodeError, ValueError):
		parsed = None
	# The stored payload is the contract dict {summary, overall, findings: [...]};
	# tolerate a bare list for older rows.
	if isinstance(parsed, dict):
		findings = parsed.get("findings")
		findings = findings if isinstance(findings, list) else []
	elif isinstance(parsed, list):
		findings = parsed
	else:
		findings = []
	if not (0 <= finding_index < len(findings)):
		raise BadRequest(f"finding_index {finding_index} out of range (report has {len(findings)} finding(s)).")
	updated_by = str(payload.get("updated_by") or frappe.session.user or "").strip()
	from frappe.utils import now

	finding = dict(findings[finding_index])
	finding["verdict"] = verdict
	finding["verdict_updated_by"] = updated_by or None
	finding["verdict_updated_at"] = now()
	findings[finding_index] = finding
	new_blob = parsed if parsed is not None else []
	if isinstance(new_blob, dict):
		new_blob["findings"] = findings
	else:
		new_blob = findings
	frappe.db.set_value(REPORT, name, "findings", json.dumps(new_blob, ensure_ascii=False), update_modified=False)
	return APIResponse.success(
		data={
			"name": name,
			"finding_index": finding_index,
			"tag_code": finding.get("tag_code"),
			"verdict": verdict,
			"verdict_updated_by": finding.get("verdict_updated_by"),
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
