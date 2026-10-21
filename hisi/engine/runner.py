"""Autonomous 10-stage SHA journey runner (background jobs).

Stage execution is pluggable: a conformance harness app can register
executors through the ``hisi_stage_executors`` hook::

    hisi_stage_executors = {"STAGE-03": "my_app.harness.benefits_check"}

An executor receives ``(context: dict)`` and returns a dict with ``status``
(one of STEP_STATUSES), ``observed_result`` and optionally
``correlation_id``. STAGE-00 ships a built-in TLS/FHIR preflight. Stages
without a registered executor are marked "Needs reviewer" — HISI never
fabricates a Pass.

Background jobs commit after each stage so the portal can show live progress;
this is deliberate and limited to the job process.
"""

import time

import frappe
from frappe.utils import now_datetime

from hisi.constants import criteria_reference, journey_stages_reference, stage_reference

_STATUS_RANK = {
	"Fail": 6,
	"Blocked": 5,
	"Needs reviewer": 4,
	"Running": 3,
	"Waiting": 2,
	"Skipped": 1,
	"Pass": 0,
}
_NO_EXECUTOR = (
	"No automated conformance executor is configured for this stage on this HISI host; "
	"reviewer verification required."
)


def new_stage_rows() -> list[dict]:
	return [
		{
			"stage_number": s["stageNumber"],
			"code": s["code"],
			"title": s["title"],
			"status": "Waiting",
			"observed_result": "Queued",
			"expected_result": s["expectedResult"],
			"correlation_id": s["correlationId"],
			"attempts": 0,
		}
		for s in journey_stages_reference()
	]


# ---------------------------------------------------------------- executors


def preflight_executor(context: dict) -> dict:
	from hisi.engine.net import UnsafeURLError, probe_fhir_metadata

	try:
		probe = probe_fhir_metadata(context["environment_url"])
	except UnsafeURLError as exc:
		return {"status": "Blocked", "observed_result": f"Target rejected by sandbox allowlist: {exc}"}

	if not probe.get("ok"):
		return {"status": "Fail", "observed_result": probe.get("detail") or f"HTTP {probe.get('status')}"}
	if probe.get("fhir"):
		observed = f"TLS verified; FHIR CapabilityStatement {probe['fhir']} published at /metadata"
		return {"status": "Pass", "observed_result": observed}
	return {
		"status": "Needs reviewer",
		"observed_result": f"TLS verified (HTTP {probe.get('status')}); no FHIR CapabilityStatement at /metadata",
	}


def _executor_for(code: str):
	# Dict hooks are merged by Frappe into {key: [path, ...]}; the last app wins.
	hooks = frappe.get_hooks("hisi_stage_executors") or {}
	path = hooks.get(code) if isinstance(hooks, dict) else None
	if isinstance(path, list):
		path = path[-1] if path else None
	if path:
		return frappe.get_attr(path)
	if code == "STAGE-00":
		return preflight_executor
	return None


def _run_stage(context: dict, code: str) -> dict:
	executor = _executor_for(code)
	if not executor:
		return {"status": "Needs reviewer", "observed_result": _NO_EXECUTOR}
	try:
		result = executor(context) or {}
	except Exception:
		frappe.log_error(title=f"HISI stage executor failed: {code}")
		return {"status": "Fail", "observed_result": "Stage executor raised an error; see Error Log."}
	status = result.get("status")
	if status not in _STATUS_RANK:
		status = "Needs reviewer"
	return {
		"status": status,
		"observed_result": str(result.get("observed_result") or "")[:1000],
		"correlation_id": result.get("correlation_id"),
	}


# ---------------------------------------------------------------- jobs


def _is_cancelled(run_name: str) -> bool:
	return frappe.db.get_value("HISI Test Run", run_name, "status") == "Cancelled"


def _write_stage(row_name: str, values: dict):
	frappe.db.set_value("HISI Test Run Stage", row_name, values, update_modified=False)


def _context(run) -> dict:
	return {
		"run": run.name,
		"validation_case": run.validation_case,
		"vendor": run.vendor,
		"environment_url": run.environment_url,
		"synthetic_pack": run.synthetic_pack,
		"release_version": run.release_version,
	}


def execute_run(run_name: str):
	run = frappe.get_doc("HISI Test Run", run_name)
	if run.status != "Queued":
		return
	run.db_set({"status": "Running", "started_on": run.started_on or now_datetime()})
	frappe.db.commit()

	context = _context(run)
	blocked_reason = None
	for row in sorted(run.stages, key=lambda r: r.stage_number):
		if _is_cancelled(run_name):
			break
		if blocked_reason:
			_write_stage(
				row.name,
				{"status": "Blocked", "observed_result": blocked_reason, "finished_on": now_datetime()},
			)
			continue
		_write_stage(row.name, {"status": "Running", "observed_result": "Running"})
		frappe.db.commit()

		started = time.monotonic()
		result = _run_stage(context, row.code)
		values = {
			"status": result["status"],
			"observed_result": result["observed_result"],
			"duration_ms": int((time.monotonic() - started) * 1000),
			"attempts": (row.attempts or 0) + 1,
			"finished_on": now_datetime(),
		}
		if result.get("correlation_id"):
			values["correlation_id"] = str(result["correlation_id"])[:140]
		_write_stage(row.name, values)
		row.status = result["status"]

		if row.code == "STAGE-00":
			_update_profile_preflight(run, result)
			if result["status"] in ("Fail", "Blocked"):
				blocked_reason = "Blocked: environment preflight did not pass"
		frappe.db.commit()

	if _is_cancelled(run_name):
		_finish_case(run, cancelled=True)
		frappe.db.commit()
		return

	run.reload()
	run.db_set({"status": "Completed", "completed_on": now_datetime()})
	_record_stage_findings(run)
	_finish_case(run)
	frappe.db.commit()


def execute_stage_retest(run_name: str, code: str):
	run = frappe.get_doc("HISI Test Run", run_name)
	row = next((r for r in run.stages if r.code == code), None)
	if not row:
		return
	started = time.monotonic()
	result = _run_stage(_context(run), code)
	_write_stage(
		row.name,
		{
			"status": result["status"],
			"observed_result": result["observed_result"],
			"duration_ms": int((time.monotonic() - started) * 1000),
			"attempts": (row.attempts or 0) + 1,
			"finished_on": now_datetime(),
		},
	)
	if code == "STAGE-00":
		_update_profile_preflight(run, result)
	run.reload()
	if result["status"] == "Pass":
		for name in frappe.get_all(
			"HISI Finding",
			filters={
				"validation_case": run.validation_case,
				"source": "Automated run",
				"trace_key": code,
				"status": ["in", ["Open", "Remediation submitted"]],
			},
			pluck="name",
		):
			frappe.db.set_value("HISI Finding", name, "status", "Retest ready")
	else:
		_record_stage_findings(run)
	_finish_case(run)
	frappe.db.commit()


# ---------------------------------------------------------------- effects


def _update_profile_preflight(run, result: dict):
	status = {"Pass": "Ready", "Needs reviewer": "Ready"}.get(result["status"], "Failed")
	for name in frappe.get_all(
		"HISI Integration Profile",
		filters={"vendor": run.vendor, "sandbox_base_url": run.environment_url},
		pluck="name",
	):
		frappe.db.set_value(
			"HISI Integration Profile",
			name,
			{
				"preflight_status": status,
				"last_verified": now_datetime(),
				"preflight_note": result["observed_result"][:500],
			},
		)


def _record_stage_findings(run):
	for row in run.stages:
		if row.status != "Fail":
			continue
		reference = f"{run.name}:{row.code}"
		if frappe.db.exists(
			"HISI Finding", {"validation_case": run.validation_case, "source_reference": reference}
		):
			continue
		stage = stage_reference(row.code) or {}
		criteria = [t for t in stage.get("traceability", []) if t.startswith("CC-")] or ["CC-01"]
		frappe.get_doc(
			{
				"doctype": "HISI Finding",
				"validation_case": run.validation_case,
				"vendor": run.vendor,
				"criterion_id": criteria[0],
				"trace_key": row.code,
				"severity": "High",
				"status": "Open",
				"source": "Automated run",
				"source_reference": reference,
				"title": f"{row.code} {row.title} failed",
				"observed_behavior": row.observed_result,
				"expected_behavior": row.expected_result,
				"owner_role": "Vendor Technical Lead",
				"due_date": frappe.utils.add_days(frappe.utils.today(), 14),
			}
		).insert(ignore_permissions=True)


def aggregate_criterion_status(run) -> dict[str, str]:
	"""Worst stage status per criterion, via the stage traceability matrix."""
	by_code = {r.code: r.status for r in run.stages} if run else {}
	out = {}
	for crit in criteria_reference():
		statuses = [
			by_code.get(s["code"], "Waiting")
			for s in journey_stages_reference()
			if crit["id"] in s.get("traceability", [])
		]
		if not statuses:
			out[crit["id"]] = crit["automatedStatus"]
			continue
		out[crit["id"]] = max(statuses, key=lambda s: _STATUS_RANK.get(s, 2))
	return out


def _finish_case(run, cancelled: bool = False):
	case = frappe.get_doc("HISI SHA Validation Case", run.validation_case)
	statuses = aggregate_criterion_status(run)
	for row in case.criteria_assessments:
		if row.criterion_id in statuses:
			frappe.db.set_value(
				"HISI Case Criterion Assessment",
				row.name,
				"automated_status",
				statuses[row.criterion_id],
				update_modified=False,
			)
	if case.state == "Running" or case.state == "Retest requested":
		state = "Ready to test" if cancelled else "Results available"
		case.db_set({"state": state, "next_action": None})
