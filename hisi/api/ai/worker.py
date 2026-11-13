# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""Async worker for the ``HISI Discrepancy Report`` job.

The job mirrors the VidVal VideoJob lifecycle: the API creates the job
document in ``Queued`` and enqueues :func:`run_discrepancy_report`; the
worker atomically claims it (one guarded ``Queued -> Processing`` UPDATE),
fetches the member's data through the ``patient_flow`` step functions,
streams the LLM call, and writes the terminal state with one guarded
``Processing -> Success|Failed`` UPDATE.

Retry policy (the VidVal AD-12 pattern, simplified):

- transport failure (HTTP error / connection error / timeout) → exactly
  ONE transport retry; a second failure → ``Failed`` with ``LLM_HTTP_ERROR``
  or ``LLM_TIMEOUT``.
- parse failure (reply not the contract JSON) → exactly ONE corrective
  retry (the raw reply echoed as an ``assistant`` turn + the fixed
  corrective message); still failing → ``Failed`` with ``LLM_PARSE_FAILED``
  (raw reply + prompt retained for audit).

Fail-closed: an unconfigured LLM host fails the job immediately with
``LLM_NOT_CONFIGURED``; a run in which every data source errored fails with
``DATA_UNAVAILABLE`` before any LLM spend.

The daily ``sweep_stuck_reports`` (registered in ``hooks.scheduler_events``)
marks ``Processing`` reports older than 2 x total_timeout as ``Failed`` /
``WORKER_CRASHED`` and ``Queued`` reports older than 4 x total_timeout as
``Failed`` / ``WORKER_LOST`` — a crashed or job-losing worker is detectable
instead of leaving a report stuck in a pre-terminal state.
"""

import hashlib
import json
import time

import frappe
from frappe.utils import add_to_date, now_datetime

from hisi.api.ai import llm_client, report_pdf
from hisi.api.ai.benefits_exchange import get_benefits_profiles
from hisi.api.ai.benefits_registry import matched_benefits_lines
from hisi.api.ai.prompt import (
	CORRECTIVE_MESSAGE,
	build_data_context,
	build_report_prompt,
	normalize_findings,
	parse_llm_json,
	render_report_markdown,
)
from hisi.api.ai.report_settings import (
	get_report_settings,
	redaction_policy,
	resolve_ai_mode,
)
from hisi.api.ai.rules import RuleContext, run_rules
from hisi.api.ai.rules.engine import CATALOG_VERSION
from hisi.api.ai.rules.redact import redact_context_for_llm
from hisi.api.ai.ai_stages import run_narrative_stage
from hisi.api.ai.settings import get_ai_data_sources, get_ai_settings
from hisi.api.patient import (
	fetch_patient_contributions,
	med_claim_search_path,
	med_preauth_search_path,
	step_claims,
	step_consent,
	step_eligibility,
	step_patient,
	step_preauth,
	step_records_multi,
	visit_claim_context,
)
from hisi.api.response_handler import BadRequest, NotFound, UpstreamError

REPORT = "HISI Discrepancy Report"

#: AD-4 sweep window: a ``Processing`` report is stuck once 2 x
#: total_timeout_sec have passed since it started.
_STUCK_MULTIPLIER = 2

#: A ``Queued`` report is lost once 4 x total_timeout_sec have passed since
#: creation: a claim normally lands within seconds, and even a legitimately
#: queued job waits at most one in-flight run (the single worker's queue slot
#: is bounded by total_timeout_sec) — beyond that, the enqueued job was never
#: claimed (worker died pre-claim, or the job was lost from the queue).
_QUEUED_STUCK_MULTIPLIER = 2 * _STUCK_MULTIPLIER


class _WorkerFailure(Exception):
	"""A closed-set worker failure: ``(code, message)`` for the error field."""

	def __init__(self, code: str, message: str):
		self.code = code
		self.message = message
		super().__init__(f"{code}: {message}")


def run_discrepancy_report(report_name: str) -> None:
	"""The enqueued entry point — claim, fetch, prompt, call, normalize, finish."""
	doc = _load(report_name)
	if doc is None:
		frappe.logger("hisi.ai").warning(f"run_discrepancy_report: {report_name!r} is gone; skipping")
		return
	if not _claim_processing(report_name):
		frappe.logger("hisi.ai").info(f"run_discrepancy_report: {report_name!r} not claimable; skipping")
		return
	# Persist the claim now instead of at the terminal update: the LLM stage runs
	# for minutes, and a worker killed mid-run used to roll the claim back — the
	# row sat ``Queued`` forever and the sweep never saw it. Committed, an
	# in-flight run reads ``Processing`` in the UI and a crashed worker leaves a
	# sweep-able row.
	if not frappe.flags.in_test:
		frappe.db.commit()
	settings = get_ai_settings()
	t0 = time.monotonic()

	def duration_ms() -> int:
		return int((time.monotonic() - t0) * 1000)

	try:
		sources, results, errors = _fetch_context(doc)
		if not results:
			detail = "; ".join(f"{key}: {value}" for key, value in errors.items())
			raise _WorkerFailure("DATA_UNAVAILABLE", f"every data source errored: {detail}")
		tags = _declared_tags(doc)
		mode = resolve_ai_mode(str(getattr(doc, "ai_mode", "") or ""))
		if mode in ("deterministic_only", "deterministic_plus_narrative", "autonomous_full"):
			_run_deterministic(doc, report_name, settings, sources, results, errors, tags, mode, duration_ms)
		else:
			_run_legacy_llm(doc, report_name, settings, sources, results, errors, tags, duration_ms)
	except _WorkerFailure as exc:
		_mark_failed(report_name, exc.code, exc.message, duration_ms())
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"run_discrepancy_report {report_name}")
		_mark_failed(report_name, "INTERNAL_ERROR", "unexpected worker failure", duration_ms())


def _run_legacy_llm(doc, report_name: str, settings, sources: list, results: dict, errors: dict, tags: list, duration_ms):
	"""The pre-v2 pipeline: one big LLM call over the data context (kept for
	A/B + transition — ``ai_mode = legacy_llm``)."""
	context = build_data_context(
		str(doc.subject_cr_id or ""), sources, results, errors, int(settings.discrepancy_max_records)
	)
	# Code-driven injection: registry rows for the intervention/document
	# codes actually present in the data (empty string when none match).
	detail = matched_benefits_lines(
		json.dumps(context, indent=2, ensure_ascii=False, default=str)
	)
	prompt = build_report_prompt(
		settings.discrepancy_system_prompt,
		context,
		tags,
		benefits_reference=settings.benefits_reference,
		benefits_detail=detail,
	)

	# Pin the generation-time state. Each db_set persists its single field
	# directly (targeted UPDATE) — deliberately NO doc.db_update() here:
	# the in-memory doc still carries the pre-claim status, and a full
	# rewrite would clobber the atomic claim's Processing state.
	doc.db_set("prompt_snapshot", prompt, update_modified=False)
	doc.db_set("data_sources", ",".join(sources), update_modified=False)
	patient_result = results.get("patient")
	if isinstance(patient_result, dict) and str(patient_result.get("name") or "").strip():
		doc.db_set("subject_name", str(patient_result.get("name")).strip(), update_modified=False)

	messages = [{"role": "user", "content": prompt}]
	try:
		raw, usage = _call_llm(settings, messages)
	except llm_client.LLMError:
		# exactly ONE transport retry
		try:
			raw, usage = _call_llm(settings, messages)
		except llm_client.LLMError as exc:
			raise _WorkerFailure(_llm_error_code(exc), _llm_error_message(exc)) from exc
	result = parse_llm_json(raw)
	if result is None:
		# exactly ONE corrective JSON retry
		corrective = messages + [
			{"role": "assistant", "content": raw or ""},
			{"role": "user", "content": CORRECTIVE_MESSAGE},
		]
		try:
			raw, usage = _call_llm(settings, corrective)
		except llm_client.LLMError as exc:
			raise _WorkerFailure(_llm_error_code(exc), _llm_error_message(exc)) from exc
		result = parse_llm_json(raw)
		if result is None:
			_mark_failed(
				report_name,
				"LLM_PARSE_FAILED",
				"the LLM reply was not valid contract JSON after one corrective retry",
				duration_ms(),
				raw_response=raw,
				usage=usage,
			)
			return

	normalized = normalize_findings(result)
	markdown = render_report_markdown(
		str(doc.subject_cr_id or ""),
		str(doc.subject_name or ""),
		normalized,
	)
	_mark_success(
		report_name,
		duration_ms(),
		pdf_bytes=_render_pdf(doc, normalized, duration_ms, sources),
		report=markdown,
		findings=json.dumps(normalized, ensure_ascii=False),
		raw_response=raw or "",
		usage=json.dumps(usage) if usage else None,
		model=str(settings.llm_model or ""),
		ai_mode_used="legacy_llm",
	)


def _scope_results(results: dict, scoped_ctx) -> dict:
	"""For an entity-scoped report, narrow the raw fetch results so the LLM
	narrative digest covers only the scoped records (the scoped context's
	claims and preauths). Member-level steps (patient, eligibility,
	contributions, consent) pass through unchanged."""
	if not results:
		return results
	claim_names = {str(c.get("name") or "").strip() for c in scoped_ctx.claims}
	preauth_names = {str(p.get("name") or "").strip() for p in scoped_ctx.preauths}
	out = dict(results)
	claims = results.get("claims")
	if isinstance(claims, dict) and isinstance(claims.get("records"), list):
		out["claims"] = dict(claims)
		out["claims"]["records"] = [
			row for row in claims["records"] if isinstance(row, dict) and str(row.get("name") or "").strip() in claim_names
		]
	preauth = results.get("preauth")
	if isinstance(preauth, dict) and isinstance(preauth.get("records"), list):
		out["preauth"] = dict(preauth)
		out["preauth"]["records"] = [
			row for row in preauth["records"] if isinstance(row, dict) and str(row.get("name") or "").strip() in preauth_names
		]
	return out


def _data_as_of(results: dict) -> str | None:
	"""The newest timestamp the fetched context saw (max of record creation /
	service dates across every lane). Stored on the job as ``data_as_of`` so
	the UI can mark a report stale the moment newer activity lands. Returns
	None when no lane carried a parseable date."""
	date_keys = ("creation", "creation_date", "date_start", "date_end", "service_date", "period")
	stamps: list[str] = []

	def _walk(value) -> None:
		if isinstance(value, dict):
			for key in date_keys:
				raw = value.get(key)
				if isinstance(raw, str) and raw.strip():
					# Keep the date part (YYYY-MM-DD) — the marker is a date-grain
					# freshness signal, not an exact timestamp.
					stamps.append(raw.strip()[:10])
				continue
			for v in value.values():
				_walk(v)
		elif isinstance(value, list):
			for item in value:
				_walk(item)

	for lane in results.values():
		_walk(lane)
	if not stamps:
		return None
	best = max(stamps)
	return best if len(best) == 10 else None


def _run_deterministic(doc, report_name: str, settings, sources: list, results: dict, errors: dict, tags: list, mode: str, duration_ms):
	"""The v2 pipeline: deterministic rules -> evidence verification ->
	policy -> (optional) routed AI narrative -> render. The LLM never detects
	or decides; findings and severity are computed in code and every finding
	is evidence-verified before it may enter the deliverable."""
	cr_id = str(doc.subject_cr_id or "").strip()
	report_settings = get_report_settings()
	ctx = RuleContext.build(cr_id, sources, results, errors)

	# Claim scoping: when the job names a single claim, the report becomes a
	# payment-decision report for that claim only (its related preauths, the
	# member's eligibility/contributions kept for context).
	# Scoping: the job names one entity (claim / visit / preauth) or none
	# (the member's full history). A scoped report narrows both the rule
	# context and the narrative digest to that entity (+ its related
	# records); member-level context stays for context.
	claim_ref = str(getattr(doc, "subject_claim", "") or "").strip()
	visit_ref = str(getattr(doc, "subject_visit", "") or "").strip()
	preauth_ref = str(getattr(doc, "subject_preauth", "") or "").strip()
	scope_kind = "claim" if claim_ref else "visit" if visit_ref else "preauth" if preauth_ref else ""
	scoped_entity = None
	if scope_kind == "claim":
		ctx, scoped_entity = ctx.scope_to_claim(claim_ref)
	elif scope_kind == "visit":
		ctx, scoped_entity = ctx.scope_to_visit(visit_ref)
	elif scope_kind == "preauth":
		ctx, scoped_entity = ctx.scope_to_preauth(preauth_ref)

	# Benefits surface: static matrix by default; live BenefitsExchange
	# (query by intervention code) overrides per code when configured.
	warnings: list = []
	if scope_kind and scoped_entity is None:
		warnings.append(
			f"subject {scope_kind} {claim_ref or visit_ref or preauth_ref!r} not found in the fetched records — reporting on the member's full history instead"
		)
	if str(report_settings.benefits_mode or "").lower() == "live":
		profiles, benefits_source, live_warnings = get_benefits_profiles(ctx.all_intervention_codes(), report_settings)
		warnings.extend(live_warnings)
		ctx.registry = {**ctx.registry, **profiles}
	else:
		benefits_source = "static" if ctx.registry else "none"

	engine_out = run_rules(ctx, tags, report_settings)
	warnings.extend(engine_out["warnings"])
	result = engine_out["result"]

	# Coverage: what the analysis could actually see, per lane (available /
	# empty / errored + counts) — rendered as its own report section so a
	# clean verdict always states its caveats. The contribution ledger is
	# carried for the same: grouped, context-only, never a finding source.
	scope_label = {
		"claim": f"Single claim — {claim_ref}",
		"visit": f"Single visit — {visit_ref}",
		"preauth": f"Single preauthorization — {preauth_ref}",
	}.get(scope_kind, "Member full history")
	coverage = ctx.coverage(scope_label)
	contribution_ledger = ctx.contribution_ledger()

	# The optional narrative stage: redacted digest in, validated prose out;
	# any failure keeps the deterministic summary (the report never blocks on
	# the model).
	models_used: list = []
	if mode in ("deterministic_plus_narrative", "autonomous_full"):
		scoped_results = _scope_results(results, ctx) if scope_kind else results
		data_context = build_data_context(cr_id, sources, scoped_results, errors, int(settings.discrepancy_max_records))
		redacted = redact_context_for_llm(data_context, redaction_policy(report_settings))
		stage = run_narrative_stage(
			report_settings,
			settings,
			cr_id,
			redacted,
			result,
			mode=mode,
			model_pref=str(getattr(doc, "model_pref", "") or "auto"),
		)
		warnings.extend(stage["warnings"])
		if stage.get("model"):
			models_used.append(str(stage["model"]))
		if stage.get("variants"):
			for key in ("model_clinical", "model_general"):
				name = str(stage["variants"].get(key) or "")
				if name and name not in models_used:
					models_used.append(name)
			result["narrative_variants"] = stage["variants"]
		if stage.get("usage"):
			usage = stage["usage"]
		if stage.get("result"):
			result["summary"] = stage["result"]["summary"]
			result["executive_summary"] = stage["result"]["executive_summary"]
		# G6: the narrative state is surfaced in the report itself (a skipped
		# or rejected narrative must never be silent — the audit line is not
		# enough; the reader sees the deterministic summary is standing in).
		if not stage.get("result"):
			narrative_state = str(stage["warnings"][0]) if stage.get("warnings") else "no output"
		else:
			narrative_state = None
	else:
		narrative_state = None

	# Pin the generation-time state (targeted field updates — same pattern as
	# the legacy path; no doc.db_update() to clobber the atomic claim).
	doc.db_set("data_sources", ",".join(sources), update_modified=False)
	patient_result = results.get("patient")
	if isinstance(patient_result, dict) and str(patient_result.get("name") or "").strip():
		doc.db_set("subject_name", str(patient_result.get("name")).strip(), update_modified=False)
	blob = json.dumps(
		{"sources": sources, "results": results, "errors": {k: str(v) for k, v in errors.items()}},
		sort_keys=True,
		ensure_ascii=False,
		default=str,
	)
	context_hash = hashlib.sha256(blob.encode("utf-8")).hexdigest()
	audit = json.dumps(
		{"mode": mode, "catalog_version": CATALOG_VERSION, "benefits_source": benefits_source, "warnings": warnings, "findings": engine_out["provenance"]},
		ensure_ascii=False,
		default=str,
	)
	doc.db_set("prompt_snapshot", audit, update_modified=False)

	markdown = render_report_markdown(cr_id, str(doc.subject_name or ""), result, coverage=coverage, contribution_ledger=contribution_ledger, narrative_state=narrative_state)
	_mark_success(
		report_name,
		duration_ms(),
		pdf_bytes=_render_pdf(doc, result, duration_ms, sources, scoped_entity, coverage=coverage, contribution_ledger=contribution_ledger, narrative_state=narrative_state),
		report=markdown,
		findings=json.dumps(result, ensure_ascii=False),
		usage=json.dumps(stage["usage"], ensure_ascii=False) if (mode in ("deterministic_plus_narrative", "autonomous_full") and stage.get("usage")) else None,
		model=",".join(models_used) or "deterministic",
		ai_mode_used=mode,
		catalog_version=CATALOG_VERSION,
		benefits_source=benefits_source,
		models_used=",".join(models_used) or "",
		data_context_hash=context_hash,
		findings_provenance=audit,
		data_as_of=_data_as_of(results),
		coverage=json.dumps(coverage, ensure_ascii=False),
	)


def _render_pdf(doc, result: dict, duration_ms, sources: list, scoped_claim=None, coverage: dict | None = None, contribution_ledger: list | None = None, narrative_state: str | None = None):
	"""Best-effort PDF render for a run (both pipelines): a host missing the
	PDF engine degrades to a report without a PDF file, never a failure."""
	try:
		status_value = str((scoped_claim or {}).get("claim_status") or "").strip() if scoped_claim else ""
		status_label = ""
		if status_value:
			row = frappe.db.get_value("HISI Claim Status", status_value, "label")
			status_label = str(row or "").strip()
		subject_preauth = str(getattr(doc, "subject_preauth", "") or "").strip()
		return report_pdf.build_report_pdf_bytes(
			str(doc.subject_cr_id or ""),
			str(doc.subject_name or ""),
			result,
			duration_ms=duration_ms(),
			data_sources=",".join(sources),
			report_ref=doc.name,
			generated_at=str(frappe.utils.now()),
			subject_claim=str(getattr(doc, "subject_claim", "") or "").strip(),
			subject_visit=str(getattr(doc, "subject_visit", "") or "").strip(),
			subject_preauth=subject_preauth,
			claim_status=status_value,
			claim_status_label=status_label,
			coverage=coverage,
			contribution_ledger=contribution_ledger,
			narrative_state=narrative_state,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"report pdf render {doc.name}")
		return None


def sweep_stuck_reports() -> int:
	"""Mark stuck reports ``Failed`` (daily) — the pre-terminal states the
	worker lifecycle can orphan:

	- ``Processing`` older than 2 x total_timeout → ``WORKER_CRASHED`` (compared
	  against ``started_at`` — the atomic claim wrote it — falling back to the
	  doc ``modified`` timestamp when it is null).
	- ``Queued`` older than 4 x total_timeout → ``WORKER_LOST`` (the job was
	  never claimed: the worker died before the claim could land, or the
	  enqueued job was lost from the queue; nothing else would ever pick it up,
	  so a plain ``Queued`` badge would mislead forever).

	One guarded UPDATE per report (the ``status`` guard keeps the sweep
	idempotent and race-safe); a second sweep finds nothing → 0.
	"""
	settings = get_ai_settings()
	threshold_sec = _STUCK_MULTIPLIER * int(settings.llm_total_timeout_sec)
	cutoff = add_to_date(now_datetime(), seconds=-threshold_sec)
	candidates = frappe.get_all(
		REPORT,
		filters={"status": "Processing"},
		fields=["name", "started_at", "modified"],
	)
	swept = 0
	for row in candidates:
		ts = row["started_at"] or row["modified"]
		if ts is None or ts >= cutoff:
			continue
		error = json.dumps(
			{
				"code": "WORKER_CRASHED",
				"message": (
					f"report stuck in Processing for more than 2 x total_timeout_sec "
					f"({threshold_sec}s) without a terminal state; worker presumed crashed (daily sweep)"
				),
			}
		)
		affected = frappe.db.execute_query(
			"UPDATE `tabHISI Discrepancy Report` SET status = %s, error = %s, finished_at = %s WHERE name = %s AND status = %s",
			("Failed", error, now_datetime(), row["name"], "Processing"),
		)
		if affected:
			swept += 1
	# Stale ``Queued`` rows: the job was never claimed (worker died before the
	# claim, or the enqueued job was lost) — nothing else would ever pick it up.
	queued_threshold = _QUEUED_STUCK_MULTIPLIER * int(settings.llm_total_timeout_sec)
	queued_cutoff = add_to_date(now_datetime(), seconds=-queued_threshold)
	for row in frappe.get_all(REPORT, filters={"status": "Queued"}, fields=["name", "creation"]):
		if row["creation"] is None or row["creation"] >= queued_cutoff:
			continue
		error = json.dumps(
			{
				"code": "WORKER_LOST",
				"message": (
					f"report sat in Queued for more than {_QUEUED_STUCK_MULTIPLIER} x total_timeout_sec "
					f"({queued_threshold}s) without a worker claim; the enqueued job was lost (daily sweep)"
				),
			}
		)
		affected = frappe.db.execute_query(
			"UPDATE `tabHISI Discrepancy Report` SET status = %s, error = %s, finished_at = %s WHERE name = %s AND status = %s",
			("Failed", error, now_datetime(), row["name"], "Queued"),
		)
		if affected:
			swept += 1
	if swept and not frappe.flags.in_test:
		frappe.db.commit()
		frappe.logger("hisi.ai").info(f"sweep_stuck_reports: marked {swept} stuck report(s) failed")
	return swept


def _load(report_name: str):
	try:
		return frappe.get_doc(REPORT, report_name)
	except frappe.DoesNotExistError:
		return None


def _claim_processing(report_name: str) -> bool:
	"""The atomic claim — one guarded UPDATE; only the first claimant wins."""
	affected = frappe.db.execute_query(
		"UPDATE `tabHISI Discrepancy Report` SET status = %s, started_at = %s WHERE name = %s AND status = %s",
		("Processing", now_datetime(), report_name, "Queued"),
	)
	return bool(affected)


def _fetch_context(doc) -> tuple:
	"""Fetch the configured data sources in-process (the patient_flow seam).

	Every step is error-isolated: a failing source lands in ``errors`` (the
	model sees it in the context and omits its findings, accuracy rule 4)
	without aborting the run.
	"""
	cr_id = str(doc.subject_cr_id or "").strip()
	sources = get_ai_data_sources()
	results: dict = {}
	errors: dict = {}
	for step in sources:
		try:
			if step == "patient":
				results[step] = step_patient({"identification_type": "CR Number", "identification_number": cr_id})
			elif step == "eligibility":
				results[step] = step_eligibility(cr_id, {})
			elif step == "consent":
				results[step] = step_consent(cr_id)
			elif step == "contributions":
				results[step] = fetch_patient_contributions(cr_id)
			elif step == "claims":
				results[step] = step_claims(cr_id)
			elif step == "preauth":
				results[step] = step_preauth(cr_id)
			elif step == "records":
				consent_result = results.get("consent")
				results[step] = step_records_multi(
					{}, {"consent": consent_result if isinstance(consent_result, dict) else {}}, cr_id
				)
			else:
				errors[step] = f"unknown step {step!r}"
		except (BadRequest, NotFound, UpstreamError) as exc:
			errors[step] = exc.message or str(exc)
		except Exception as exc:
			errors[step] = str(exc)
	_attach_visit_links(results, errors)
	return sources, results, errors


def _attach_visit_links(results: dict, errors: dict) -> None:
	"""The claim<->visit join for the per-visit comparison (see
	``visit_claim_context``): each claim's ``bundle_id`` points at the Shared
	Health Record the claim was submitted in, and that SHR's FHIR bundle
	carries the Encounter whose ``episodeOfCare`` is the visit (the consent
	``visit_id`` space). The submitted bundles become the SHR side of the
	per-visit comparison and land in ``results["visits"]`` alongside the
	consented visits. Claim-driven: it runs whenever any claim/preauth
	record is present, even if the consent step failed. A claim whose
	visit cannot be resolved (a bundle with no readable encounter) is
	still compared — grouped under a synthetic ``unknown-visitId-<date>``
	bucket joined with the claim's creation date, never dropped. Fail-soft:
	a join failure is recorded in errors and never sinks the run."""
	try:
		routes = {"claims": med_claim_search_path(), "preauth": med_preauth_search_path()}
	except (BadRequest, NotFound, UpstreamError) as exc:
		errors["visit_join"] = "claim route unavailable: {}".format(exc.message or exc)
		return
	except Exception as exc:
		errors["visit_join"] = "claim route unavailable: {}".format(exc)
		return
	claim_routes = []
	for step in ("claims", "preauth"):
		data = results.get(step)
		if not isinstance(data, dict):
			continue
		for record in data.get("records") or []:
			if isinstance(record, dict):
				claim_routes.append((record, routes[step]))
	if not claim_routes:
		return
	try:
		claim_map, by_visit, problems = visit_claim_context(claim_routes)
	except (BadRequest, NotFound, UpstreamError) as exc:
		errors["visit_join"] = exc.message or str(exc)
		return
	except Exception as exc:
		errors["visit_join"] = str(exc)
		return
	if problems:
		errors["visit_join"] = "; ".join(problems)
	for step in ("claims", "preauth"):
		data = results.get(step)
		if not isinstance(data, dict):
			continue
		for record in data.get("records") or []:
			if not isinstance(record, dict):
				continue
			visit_id = claim_map.get(str(record.get("name") or "").strip())
			if visit_id:
				record["visit_id"] = visit_id
	# Consent side: the consent step may have succeeded with zero records
	# (consented = False is then a real finding) or errored entirely
	# (consented = None — the model must not guess).
	consent = results.get("consent")
	consent_records = consent.get("records") if isinstance(consent, dict) else None
	consent_by_visit = {}
	for row in consent_records or []:
		if not isinstance(row, dict):
			continue
		visit_id = str(row.get("visit_id") or "").strip()
		if visit_id:
			consent_by_visit.setdefault(visit_id, row)
	linked_by_visit = {}
	for step in ("claims", "preauth"):
		data = results.get(step)
		if not isinstance(data, dict):
			continue
		for record in data.get("records") or []:
			if not isinstance(record, dict):
				continue
			# A claim whose bundle carries no readable encounter is still
			# compared — grouped under its creation date, not dropped.
			visit_id = str(record.get("visit_id") or "").strip() or _unknown_visit_key(record)
			if not visit_id:
				continue
			entry = {"kind": "preauth" if step == "preauth" else "claim"}
			entry.update(record)
			linked_by_visit.setdefault(visit_id, []).append(entry)
	visits = []
	for visit_id, row in consent_by_visit.items():
		visits.append(
			_visit_entry(
				visit_id,
				consented=True,
				facility_id=row.get("facility_id"),
				consent_status=row.get("consent_status"),
				consent_creation=row.get("creation"),
				shr_resources=by_visit.get(visit_id) or [],
				linked_claims=linked_by_visit.get(visit_id) or [],
			)
		)
	for visit_id in sorted((set(linked_by_visit) | set(by_visit)) - set(consent_by_visit)):
		visits.append(
			_visit_entry(
				visit_id,
				consented=None if consent_records is None else False,
				shr_resources=by_visit.get(visit_id) or [],
				linked_claims=linked_by_visit.get(visit_id) or [],
			)
		)
	results["visits"] = visits


def _unknown_visit_key(record: dict) -> str:
	"""A synthetic visit bucket for a claim whose visit cannot be resolved
	(no readable encounter in the claim's bundle): ``unknown-visitId-<date>``,
	joined with the claim's creation date (e.g. ``unknown-visitId-2026-12-11``).
	Empty when the record has no creation date to key on."""
	creation = str(record.get("creation") or "").strip()
	date_part = creation[:10]
	if len(date_part) == 10 and date_part[4] == "-" and date_part[7] == "-":
		return "unknown-visitId-{}".format(date_part)
	return ""


def _visit_entry(visit_id, *, consented, shr_resources, linked_claims, facility_id=None, consent_status=None, consent_creation=None) -> dict:
	"""One per-visit comparison bucket for the prompt context."""
	entry = {
		"visit_id": visit_id,
		"consented": consented,
		"shr_resources": shr_resources,
		"linked_claims": linked_claims,
	}
	if facility_id not in (None, ""):
		entry["facility_id"] = facility_id
	if consent_status not in (None, ""):
		entry["consent_status"] = consent_status
	if consent_creation not in (None, ""):
		entry["consent_creation"] = consent_creation
	return entry


def _declared_tags(doc) -> list:
	"""The tag snapshot taken at generation time (JSON in ``tag_snapshot``)."""
	snapshot = str(doc.get("tag_snapshot") or "").strip()
	if not snapshot:
		return []
	try:
		parsed = json.loads(snapshot)
	except (json.JSONDecodeError, ValueError):
		return []
	if not isinstance(parsed, list):
		return []
	return [t for t in parsed if isinstance(t, dict) and str(t.get("tag_code") or "").strip()]


def _call_llm(settings, messages: list[dict]) -> tuple[str, dict | None]:
	"""The worker's single LLM touchpoint — fail-closed when unconfigured."""
	if not settings.llm_base_url:
		raise _WorkerFailure(
			"LLM_NOT_CONFIGURED",
			"llm_base_url is not configured in HISI AI Settings; the LLM stage cannot run",
		)
	return llm_client.stream_chat_completion(
		base_url=settings.llm_base_url,
		api_key=settings.llm_api_key or "",
		model=str(settings.llm_model or "qwen3-27b"),
		messages=messages,
		idle_timeout_sec=int(settings.llm_idle_timeout_sec),
		total_timeout_sec=int(settings.llm_total_timeout_sec),
	)


def _llm_error_code(exc: Exception) -> str:
	"""Map an LLM transport failure to its closed job-time code."""
	return "LLM_TIMEOUT" if isinstance(exc, llm_client.LLMTimeout) else "LLM_HTTP_ERROR"


def _llm_error_message(exc: Exception) -> str:
	"""Upstream status/body snippet (``LLMHTTPError``) or the plain failure text."""
	if isinstance(exc, llm_client.LLMHTTPError):
		return f"HTTP {exc.status}: {exc.body_snippet}"
	return str(exc)


def _terminal_update(report_name: str, status: str, **fields) -> bool:
	"""One guarded terminal UPDATE — never clobbers a job already terminal."""
	values = {"status": status, "finished_at": now_datetime()}
	values.update({key: value for key, value in fields.items() if value is not None})
	sets = ", ".join(f"`{key}` = %s" for key in values)
	affected = frappe.db.execute_query(
		f"UPDATE `tab{REPORT}` SET {sets} WHERE name = %s AND status = %s",
		(*values.values(), report_name, "Processing"),
	)
	if not affected:
		# the row left Processing underneath us (a concurrent retry/sweep owns the
		# terminal state now) — log it rather than dropping the update silently
		frappe.logger("hisi.ai").warning(f"{report_name!r} {status} update discarded (row no longer Processing)")
	if affected and not frappe.flags.in_test:
		# commit in production so the terminal state is visible to polling
		# callers immediately; under the test runner the per-class rollback
		# owns the transaction (a commit here would leak test state).
		frappe.db.commit()
	return bool(affected)


def _mark_success(report_name: str, duration_ms: int, pdf_bytes: bytes | None = None, **fields) -> bool:
	if pdf_bytes:
		# Attach before the terminal UPDATE so both rows land in the same
		# commit (the guarded UPDATE commits in production).
		report_pdf.attach_report_pdf(report_name, pdf_bytes)
	ok = _terminal_update(report_name, "Success", duration_ms=duration_ms, **fields)
	if ok:
		frappe.logger("hisi.ai").info(f"discrepancy report {report_name!r} succeeded")
	return ok


def _mark_failed(
	report_name: str, code: str, message: str, duration_ms: int | None = None, raw_response: str | None = None, usage=None
) -> bool:
	ok = _terminal_update(
		report_name,
		"Failed",
		error=json.dumps({"code": code, "message": message}),
		duration_ms=duration_ms,
		raw_response=raw_response,
		usage=json.dumps(usage) if usage else None,
	)
	if ok:
		frappe.logger("hisi.ai").warning(f"discrepancy report {report_name!r} failed ({code})")
	return ok
