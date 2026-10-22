"""SHA claims validation workspace API (hisi.api.validation.*).

Two-track governance (HISI-FR-006): the first-round DHA certification status
(``dhaCertificate.dhaStatus``, stored on HISI DHA Certification Reference) and
the HISI SHA claims validation state (``shaState``, stored on HISI SHA
Validation Case) are independent. Nothing in this module writes to a DHA
certification record.
"""

import hashlib
import json
from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.model.naming import make_autoname
from frappe.utils import getdate, now_datetime, today

from hisi.api._common import (
	CASE,
	clean,
	default_case_name,
	fmt_date,
	fmt_dt,
	get_case,
	lock_case,
	not_found,
	ok,
	require_reviewer,
	require_user,
	require_vendor_actor,
)
from hisi.constants import (
	ADVERSE_OUTCOMES,
	CASE_STATE_TO_PORTAL,
	CRITERION_OUTCOMES,
	DHA_STATUS_TO_PORTAL,
	DISPOSITION_TO_CASE_STATE,
	DISPOSITIONS,
	FINAL_STATES,
	LEGACY_VENDOR,
	NEXT_ACTIONS,
	PASSING_OUTCOMES,
	RUNNABLE_STATES,
	VENDOR_ADMIN,
	VENDOR_ROLES,
	VENDOR_TECH,
	criterion_reference,
	fraud_controls_reference,
	journey_stages_reference,
	stage_reference,
)
from hisi.utils import get_user_orgs

TECHNICAL_VENDOR_ROLES = (VENDOR_ADMIN, VENDOR_TECH, LEGACY_VENDOR)
DEFAULT_SYNTHETIC_PACK = "SHA-SYNTH-2026.10"
_OUTCOME_RANK = {"Fail": 3, "Blocked": 2, "Partial": 1}


# ================================================================ view models


def portal_state(state: str | None) -> str:
	return CASE_STATE_TO_PORTAL.get(state or "", state or "Scope confirmation")


def dha_track(cert) -> str:
	"""Portal dhaStatus for a certificate, honouring expiry dates."""
	status = DHA_STATUS_TO_PORTAL.get(cert.certificate_status or "Unknown", "Pending verification")
	if cert.expiry_date and getdate(cert.expiry_date) < getdate(today()):
		return "Expired"
	return status


def certificate_view(cert) -> dict:
	product_name = (
		frappe.db.get_value("HISI HMIS Product", cert.product, "product_name") if cert.product else ""
	)
	vendor_name = (
		frappe.db.get_value("HISI Vendor Organization", cert.vendor, "organization_name")
		if cert.vendor
		else ""
	)
	return {
		"certificateId": cert.certificate_id,
		"systemName": product_name or "",
		"vendorName": vendor_name or "",
		"scopeType": cert.get("scope_type") or "First-round DHA HMIS Certification (Published)",
		"dhaStatus": dha_track(cert),
		"issueDate": fmt_date(cert.issue_date),
		"expiryDate": fmt_date(cert.expiry_date),
		"verifiedAt": fmt_date(cert.verified_on),
		"verifierSource": cert.get("verifier_source") or "DHA Public Register",
		"trackerUrl": frappe.conf.get("hisi_dha_tracker_url") or "https://certification.dha.go.ke/track",
		"verifyUrl": frappe.conf.get("hisi_dha_verify_url") or "https://certification.dha.go.ke/verify",
	}


def _evidence_rows(case_name: str) -> list[dict]:
	return frappe.get_all(
		"HISI Evidence Item",
		filters={"validation_case": case_name},
		fields=[
			"name",
			"criterion_id",
			"trace_key",
			"title",
			"scenario",
			"product_version",
			"environment",
			"captured_at",
			"submitted_by",
			"review_status",
			"reviewer_note",
			"checksum",
		],
		order_by="creation desc",
	)


def _finding_rows(case_name: str) -> list[dict]:
	return frappe.get_all(
		"HISI Finding",
		filters={"validation_case": case_name},
		fields=[
			"name",
			"criterion_id",
			"trace_key",
			"severity",
			"title",
			"observed_behavior",
			"expected_behavior",
			"owner_role",
			"due_date",
			"status",
			"vendor_response",
		],
		order_by="creation desc",
	)


def _latest_run(case):
	if case.get("latest_run") and frappe.db.exists("HISI Test Run", case.latest_run):
		return frappe.get_doc("HISI Test Run", case.latest_run)
	return None


def _fraud_status(linked: list[str], assessments: dict[str, str]) -> str:
	outcomes = [assessments.get(c, "Not assessed") for c in linked]
	adverse = [o for o in outcomes if o in ADVERSE_OUTCOMES]
	if adverse:
		return max(adverse, key=lambda o: _OUTCOME_RANK[o])
	if outcomes and all(o in PASSING_OUTCOMES for o in outcomes):
		if all(o == "Not applicable" for o in outcomes):
			return "Not applicable"
		return "Pass with observation" if "Pass with observation" in outcomes else "Pass"
	return "Not assessed"


def build_case_summary(case) -> dict:
	cert = frappe.get_doc("HISI DHA Certification Reference", case.dha_certificate)
	checklist = frappe.get_doc("HISI Checklist Version", case.checklist_version)
	rows = {r.criterion_id: r for r in case.criteria_assessments}
	evidence = _evidence_rows(case.name)
	findings = _finding_rows(case.name)
	run = _latest_run(case)

	criteria = []
	for crit in checklist.criteria:
		ref = criterion_reference(crit.criterion_id) or {}
		row = rows.get(crit.criterion_id)
		criteria.append(
			{
				"id": crit.criterion_id,
				"title": crit.title,
				"coverage": ref.get("coverage") or crit.statement or "",
				"evidenceMode": ref.get("evidenceMode") or "Mixed",
				"mandatoryBlocker": bool(crit.is_critical),
				"assessment": (row.assessment if row else None) or "Not assessed",
				"automatedStatus": (row.get("automated_status") if row else None)
				or ref.get("automatedStatus")
				or "Waiting",
				"manualEvidenceCount": sum(
					1
					for e in evidence
					if e.criterion_id == crit.criterion_id and e.review_status != "Rejected"
				),
				"openFindingsCount": sum(
					1 for f in findings if f.criterion_id == crit.criterion_id and f.status != "Resolved"
				),
				"owner": ref.get("owner") or "SHA Validation Reviewer",
				"rationale": (row.rationale if row and row.rationale else None) or ref.get("rationale") or "",
			}
		)

	run_rows = {r.code: r for r in run.stages} if run else {}
	journey = []
	for stage in journey_stages_reference():
		row = run_rows.get(stage["code"])
		journey.append(
			{
				"stageNumber": stage["stageNumber"],
				"code": stage["code"],
				"title": stage["title"],
				"autonomousChecks": stage["autonomousChecks"],
				"traceability": stage["traceability"],
				"status": row.status if row else "Waiting",
				"durationMs": row.duration_ms if row and row.duration_ms else None,
				"observedResult": (row.observed_result if row else None) or stage["observedResult"],
				"expectedResult": stage["expectedResult"],
				"correlationId": (row.correlation_id if row else None) or stage["correlationId"],
			}
		)

	assessed = {c["id"]: c["assessment"] for c in criteria}
	fraud = [
		{**fc, "status": _fraud_status(fc["linkedCriteria"], assessed)} for fc in fraud_controls_reference()
	]

	label, href = NEXT_ACTIONS.get(case.state, ("Open the validation case", "/certification"))
	disposition = None
	if case.get("disposition_record"):
		d = frappe.db.get_value(
			"HISI SHA Disposition",
			case.disposition_record,
			["name", "disposition", "issued_on", "issued_by", "evidence_bundle_hash", "conditions"],
			as_dict=True,
		)
		if d:
			disposition = {
				"dispositionId": d.name,
				"disposition": d.disposition,
				"issuedOn": fmt_dt(d.issued_on),
				"issuedBy": d.issued_by,
				"bundleHash": d.evidence_bundle_hash,
				"conditions": d.conditions or "",
			}

	return {
		"caseId": case.name,
		"releaseVersion": case.release_version or "",
		"checklistVersion": checklist.version,
		"scenarioVersion": case.get("scenario_version") or journey_stages_reference()[0]["correlationId"],
		"shaState": portal_state(case.state),
		"responsibleOwner": case.current_owner or "",
		"nextActionLabel": case.next_action or label,
		"nextActionHref": href,
		"dhaCertificate": certificate_view(cert),
		"criteria": criteria,
		"journeyStages": journey,
		"fraudControls": fraud,
		"evidenceItems": [
			{
				"id": e.name,
				"criterionId": e.criterion_id,
				"traceKey": e.trace_key or None,
				"title": e.title,
				"scenario": e.scenario,
				"productVersion": e.product_version,
				"environment": e.environment,
				"capturedAt": fmt_dt(e.captured_at),
				"submittedBy": e.submitted_by or "",
				"reviewStatus": e.review_status,
				"reviewerNote": e.reviewer_note or "",
				"checksum": e.checksum or "",
			}
			for e in evidence
		],
		"findings": [
			{
				"id": f.name,
				"criterionId": f.criterion_id,
				"traceKey": f.trace_key or "",
				"severity": f.severity,
				"title": f.title,
				"observedBehavior": f.observed_behavior or "",
				"expectedBehavior": f.expected_behavior or "",
				"owner": f.owner_role or "",
				"dueDate": fmt_date(f.due_date),
				"status": f.status,
				"vendorResponse": f.vendor_response or None,
			}
			for f in findings
		],
		"latestRun": (
			{
				"runId": run.name,
				"status": run.status,
				"startedOn": fmt_dt(run.started_on),
				"completedOn": fmt_dt(run.completed_on),
			}
			if run
			else None
		),
		"disposition": disposition,
	}


# ================================================================ reads


@frappe.whitelist(methods=["GET"])
def list_cases() -> dict:
	require_user()
	cases = frappe.get_list(
		CASE,
		fields=[
			"name",
			"vendor",
			"product",
			"dha_certificate",
			"release_version",
			"state",
			"current_owner",
			"modified",
		],
		order_by="modified desc",
		limit=200,
	)
	out = []
	for c in cases:
		cert = frappe.get_doc("HISI DHA Certification Reference", c.dha_certificate)
		label, href = NEXT_ACTIONS.get(c.state, ("Open the validation case", "/certification"))
		out.append(
			{
				"caseId": c.name,
				"releaseVersion": c.release_version or "",
				"shaState": portal_state(c.state),
				"dhaStatus": dha_track(cert),
				"certificateId": cert.certificate_id,
				"systemName": frappe.db.get_value("HISI HMIS Product", c.product, "product_name") or "",
				"vendorName": frappe.db.get_value("HISI Vendor Organization", c.vendor, "organization_name")
				or "",
				"responsibleOwner": c.current_owner or "",
				"nextActionLabel": label,
				"nextActionHref": href,
				"updatedAt": fmt_dt(c.modified),
			}
		)
	return ok({"cases": out})


@frappe.whitelist(methods=["GET"])
def get_case_details(case_id: str | None = None) -> dict:
	require_user()
	name = case_id or default_case_name()
	if not name:
		# No case visible to this user yet: the portal falls back to its onboarding shell.
		return ok({})
	return ok(build_case_summary(get_case(name)))


# ================================================================ vendor mutations


def _latest_published_checklist() -> str:
	rows = frappe.get_all(
		"HISI Checklist Version",
		filters={"published": 1},
		fields=["name"],
		order_by="effective_on desc, creation desc",
		limit=1,
	)
	if not rows:
		frappe.throw(_("No published SHA checklist is available. Contact the HISI administrator."))
	return rows[0].name


@frappe.whitelist(methods=["POST"])
def link_dha_certificate(
	certificate_id: str,
	system_name: str,
	vendor_name: str,
	release_version: str,
	scope_notes: str | None = None,
) -> dict:
	user = require_user()
	certificate_id = clean(certificate_id, 64)
	cert_vendor = frappe.db.get_value("HISI DHA Certification Reference", certificate_id, "vendor")
	# Never confirm the existence of another organization's certificate (CWE-639).
	if not cert_vendor or cert_vendor not in get_user_orgs(user):
		not_found("DHA certificate")
	require_vendor_actor(cert_vendor, TECHNICAL_VENDOR_ROLES)

	cert = frappe.get_doc("HISI DHA Certification Reference", certificate_id)
	product_name = frappe.db.get_value("HISI HMIS Product", cert.product, "product_name") or ""
	if product_name.casefold() != clean(system_name, 140).casefold():
		frappe.throw(_("The HMIS product name does not match the DHA certificate record."))

	dha_status = dha_track(cert)
	if dha_status == "Expired" or cert.certificate_status == "Suspended":
		frappe.throw(
			_("This DHA certificate is not active. Renew the DHA certification before SHA validation.")
		)

	release_version = clean(release_version, 64)
	existing = frappe.get_all(
		CASE,
		filters={
			"dha_certificate": cert.name,
			"release_version": release_version,
			"state": ["not in", list(FINAL_STATES)],
		},
		pluck="name",
		limit=1,
	)
	if existing:
		return ok({"caseId": existing[0], "status": "existing"})

	checklist = frappe.get_doc("HISI Checklist Version", _latest_published_checklist())
	state = (
		"Certificate verification pending" if dha_status == "Pending verification" else "Scope confirmation"
	)
	case = frappe.get_doc(
		{
			"doctype": CASE,
			"case_name": make_autoname("SHA-VAL-.YYYY.-.#####"),
			"vendor": cert.vendor,
			"product": cert.product,
			"dha_certificate": cert.name,
			"checklist_version": checklist.name,
			"release_version": release_version,
			"scope": "SHA Digital Claims workflow",
			"scope_notes": clean(scope_notes, 500),
			"state": state,
			"current_owner": user,
			"criteria_assessments": [
				{
					"criterion_id": c.criterion_id,
					"criterion_title": c.title,
					"is_critical": c.is_critical,
					"assessment": "Not assessed",
					"automated_status": (criterion_reference(c.criterion_id) or {}).get(
						"automatedStatus", "Waiting"
					),
				}
				for c in checklist.criteria
			],
		}
	)
	# Authorization performed above (org membership + vendor role); vendors have no Desk create right.
	case.insert(ignore_permissions=True)
	return ok({"caseId": case.name, "status": portal_state(case.state)})


@frappe.whitelist(methods=["POST"])
def start_test_run(case_id: str, environment_url: str, synthetic_pack: str | None = None) -> dict:
	from hisi.engine.net import normalize_sandbox_url
	from hisi.engine.runner import new_stage_rows

	case = get_case(case_id)
	user = require_vendor_actor(case.vendor, TECHNICAL_VENDOR_ROLES, allow_reviewer=True)
	lock_case(case.name)
	case.reload()

	if case.state not in RUNNABLE_STATES:
		frappe.throw(
			_("A test run cannot be started while the case is in state: {0}.").format(
				portal_state(case.state)
			)
		)
	if frappe.db.exists(
		"HISI Test Run", {"validation_case": case.name, "status": ["in", ["Queued", "Running"]]}
	):
		frappe.throw(_("A test run is already in progress for this case."))

	url = normalize_sandbox_url(clean(environment_url, 256))
	run = frappe.get_doc(
		{
			"doctype": "HISI Test Run",
			"validation_case": case.name,
			"vendor": case.vendor,
			"status": "Queued",
			"environment_url": url,
			"synthetic_pack": clean(synthetic_pack, 64) or DEFAULT_SYNTHETIC_PACK,
			"release_version": case.release_version,
			"started_by": user,
			"started_on": now_datetime(),
			"stages": new_stage_rows(),
		}
	).insert(ignore_permissions=True)

	case.db_set({"state": "Running", "latest_run": run.name, "next_action": None})
	frappe.enqueue(
		"hisi.engine.runner.execute_run",
		queue="long",
		timeout=1800,
		run_name=run.name,
		job_id=f"hisi-run::{run.name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)
	return ok({"runId": run.name, "status": "Queued"})


def _get_case_run(case, run_id: str):
	run_case = frappe.db.get_value("HISI Test Run", run_id, "validation_case")
	if run_case != case.name:
		not_found("Test run")
	return frappe.get_doc("HISI Test Run", run_id)


@frappe.whitelist(methods=["POST"])
def cancel_test_run(case_id: str, run_id: str) -> dict:
	case = get_case(case_id)
	user = require_vendor_actor(case.vendor, TECHNICAL_VENDOR_ROLES, allow_reviewer=True)
	lock_case(case.name)
	run = _get_case_run(case, clean(run_id, 64))
	if run.status not in ("Queued", "Running"):
		frappe.throw(_("Only queued or running test runs can be cancelled."))

	run.db_set({"status": "Cancelled", "cancelled_by": user, "completed_on": now_datetime()})
	for row in run.stages:
		if row.status in ("Waiting", "Running"):
			frappe.db.set_value(
				"HISI Test Run Stage", row.name, {"status": "Skipped", "observed_result": "Run cancelled"}
			)
	case.reload()
	if case.state == "Running":
		case.db_set("state", "Ready to test")
	return ok({"runId": run.name, "status": "Cancelled"})


@frappe.whitelist(methods=["POST"])
def retest_journey_step(case_id: str, step_code: str, release_version: str | None = None) -> dict:
	case = get_case(case_id)
	require_vendor_actor(case.vendor, TECHNICAL_VENDOR_ROLES, allow_reviewer=True)
	lock_case(case.name)
	case.reload()

	step_code = clean(step_code, 32).upper()
	if not stage_reference(step_code):
		frappe.throw(_("Unknown journey step: {0}").format(step_code))
	if case.state in FINAL_STATES:
		frappe.throw(_("This case already has a final SHA disposition."))
	run = _latest_run(case)
	if not run or run.status != "Completed":
		frappe.throw(_("Complete a full journey run before retesting individual steps."))

	row = next(r for r in run.stages if r.code == step_code)
	frappe.db.set_value(
		"HISI Test Run Stage", row.name, {"status": "Running", "observed_result": "Retest queued"}
	)
	updates = {"state": "Retest requested", "next_action": None}
	if release_version:
		updates["release_version"] = clean(release_version, 64)
		run.db_set("release_version", updates["release_version"])
	case.db_set(updates)

	frappe.enqueue(
		"hisi.engine.runner.execute_stage_retest",
		queue="default",
		timeout=600,
		run_name=run.name,
		code=step_code,
		job_id=f"hisi-retest::{run.name}::{step_code}",
		deduplicate=True,
		enqueue_after_commit=True,
	)
	return ok({"runId": run.name, "stepCode": step_code, "status": "Queued"})


def _checklist_criteria(case) -> dict:
	checklist = frappe.get_doc("HISI Checklist Version", case.checklist_version)
	return {c.criterion_id: c for c in checklist.criteria}


def _validate_video_reference(reference: str, user: str) -> str:
	reference = clean(reference, 256)
	if reference.startswith("/private/files/") or reference.startswith("/files/"):
		file_owner = frappe.db.get_value("File", {"file_url": reference}, "owner")
		if file_owner != user:
			frappe.throw(_("The uploaded video file was not found."))
		return reference
	parts = urlsplit(reference)
	if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
		frappe.throw(_("Provide an HTTPS link to the video evidence or upload it first."))
	return reference


def _canonical_hash(payload: dict) -> str:
	blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
	return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@frappe.whitelist(methods=["POST"])
def submit_manual_evidence(
	case_id: str,
	criterion_id: str,
	title: str,
	scenario: str,
	product_version: str,
	environment: str,
	description: str,
	video_reference: str,
) -> dict:
	case = get_case(case_id)
	user = require_vendor_actor(case.vendor, VENDOR_ROLES)
	criterion_id = clean(criterion_id, 32).upper()
	if criterion_id not in _checklist_criteria(case):
		frappe.throw(_("Criterion {0} is not part of this case's checklist.").format(criterion_id))
	if case.state in FINAL_STATES:
		frappe.throw(_("This case already has a final SHA disposition."))

	captured_at = now_datetime()
	fields = {
		"validation_case": case.name,
		"criterion_id": criterion_id,
		"title": clean(title, 140),
		"scenario": clean(scenario, 140),
		"product_version": clean(product_version, 64),
		"environment": clean(environment, 128),
		"description": clean(description, 1000),
		"video_reference": _validate_video_reference(video_reference, user),
	}
	checksum = _canonical_hash({**fields, "submitted_by": user, "captured_at": captured_at.isoformat()})
	evidence = frappe.get_doc(
		{
			"doctype": "HISI Evidence Item",
			"vendor": case.vendor,
			**fields,
			"review_status": "Pending review",
			"submitted_by": user,
			"captured_at": captured_at,
			"checksum": checksum,
		}
	).insert(ignore_permissions=True)
	return ok({"evidenceId": evidence.name, "status": evidence.review_status, "checksum": checksum})


@frappe.whitelist(methods=["POST"])
def respond_to_finding(
	case_id: str, finding_id: str, remediation_plan: str, updated_release: str | None = None
) -> dict:
	case = get_case(case_id)
	user = require_vendor_actor(case.vendor, VENDOR_ROLES)
	finding_id = clean(finding_id, 64)
	if frappe.db.get_value("HISI Finding", finding_id, "validation_case") != case.name:
		not_found("Finding")
	finding = frappe.get_doc("HISI Finding", finding_id)
	if finding.status == "Resolved":
		frappe.throw(_("This finding is already resolved."))

	finding.update(
		{
			"vendor_response": clean(remediation_plan, 1000),
			"updated_release": clean(updated_release, 64) or finding.updated_release,
			"responded_by": user,
			"responded_on": now_datetime(),
			"status": "Remediation submitted",
		}
	)
	finding.save(ignore_permissions=True)
	if case.state == "Remediation required":
		case.db_set("state", "Retest requested")
	return ok({"updated": True, "status": finding.status})


# ================================================================ reviewer mutations


def _severity_for(is_critical: bool, outcome: str) -> str:
	if is_critical and outcome in ("Fail", "Blocked"):
		return "Critical"
	if is_critical or outcome in ("Fail", "Blocked"):
		return "High"
	return "Medium"


@frappe.whitelist(methods=["POST"])
def record_criterion_assessment(case_id: str, criterion_id: str, outcome: str, rationale: str) -> dict:
	require_reviewer()
	user = frappe.session.user
	case = get_case(case_id)
	lock_case(case.name)
	case.reload()

	if outcome not in CRITERION_OUTCOMES:
		frappe.throw(_("Unknown assessment outcome."))
	if case.state in FINAL_STATES:
		frappe.throw(_("This case already has a final SHA disposition."))
	criterion_id = clean(criterion_id, 32).upper()
	crit = _checklist_criteria(case).get(criterion_id)
	if not crit:
		frappe.throw(_("Criterion {0} is not part of this case's checklist.").format(criterion_id))
	rationale = clean(rationale, 1000)
	if not rationale:
		frappe.throw(_("A rationale is required for every assessment."))

	row = next((r for r in case.criteria_assessments if r.criterion_id == criterion_id), None)
	if not row:
		row = case.append(
			"criteria_assessments",
			{"criterion_id": criterion_id, "criterion_title": crit.title, "is_critical": crit.is_critical},
		)
	row.update(
		{"assessment": outcome, "rationale": rationale, "assessed_by": user, "assessed_on": now_datetime()}
	)

	if case.state in (
		"Results available",
		"Running",
		"Ready to test",
		"Scope confirmation",
		"Retest requested",
	):
		case.state = "Manual review"
	if all(r.assessment and r.assessment != "Not assessed" for r in case.criteria_assessments):
		case.state = "SHA disposition review"
	# Reviewer role verified above; Desk write is reserved for administrators.
	case.save(ignore_permissions=True)

	if outcome in ADVERSE_OUTCOMES and not frappe.db.exists(
		"HISI Finding",
		{
			"validation_case": case.name,
			"criterion_id": criterion_id,
			"source": "Reviewer assessment",
			"status": ["!=", "Resolved"],
		},
	):
		ref = criterion_reference(criterion_id) or {}
		frappe.get_doc(
			{
				"doctype": "HISI Finding",
				"validation_case": case.name,
				"vendor": case.vendor,
				"criterion_id": criterion_id,
				"trace_key": criterion_id,
				"severity": _severity_for(bool(crit.is_critical), outcome),
				"status": "Open",
				"source": "Reviewer assessment",
				"source_reference": f"{case.name}:{criterion_id}",
				"title": f"{criterion_id} {crit.title}: {outcome}",
				"observed_behavior": rationale,
				"expected_behavior": ref.get("coverage") or crit.statement or "",
				"owner_role": ref.get("owner") or "HMIS Vendor Administrator",
				"due_date": frappe.utils.add_days(today(), 14),
			}
		).insert(ignore_permissions=True)

	return ok(
		{
			"updated": True,
			"criterionId": criterion_id,
			"outcome": outcome,
			"caseState": portal_state(case.state),
		}
	)


def _evidence_bundle(case, cert_status: str) -> dict:
	run = _latest_run(case)
	return {
		"caseId": case.name,
		"releaseVersion": case.release_version,
		"checklistVersion": case.checklist_version,
		"scenarioVersion": case.get("scenario_version"),
		"dhaCertificate": {"certificateId": case.dha_certificate, "dhaStatus": cert_status},
		"criteria": [
			{
				"id": r.criterion_id,
				"assessment": r.assessment,
				"assessedBy": r.assessed_by,
				"assessedOn": fmt_dt(r.assessed_on),
				"rationale": r.rationale,
			}
			for r in sorted(case.criteria_assessments, key=lambda r: r.criterion_id)
		],
		"run": (
			{
				"runId": run.name,
				"status": run.status,
				"stages": [
					{"code": s.code, "status": s.status, "observed": s.observed_result} for s in run.stages
				],
			}
			if run
			else None
		),
		"evidence": [
			{
				"id": e.name,
				"criterionId": e.criterion_id,
				"checksum": e.checksum,
				"reviewStatus": e.review_status,
			}
			for e in _evidence_rows(case.name)
		],
		"findings": [
			{"id": f.name, "severity": f.severity, "status": f.status} for f in _finding_rows(case.name)
		],
	}


@frappe.whitelist(methods=["POST"])
def issue_sha_disposition(
	case_id: str, disposition: str, rationale: str, conditions: str | None = None
) -> dict:
	require_reviewer()
	user = frappe.session.user
	case = get_case(case_id)
	lock_case(case.name)
	case.reload()

	if disposition not in DISPOSITIONS:
		frappe.throw(_("Unknown SHA disposition."))
	if case.state in FINAL_STATES:
		frappe.throw(_("This case already has a final SHA disposition."))
	rationale = clean(rationale, 1000)
	conditions = clean(conditions, 1000)
	if not rationale:
		frappe.throw(_("A rationale is required to issue a disposition."))

	# Read-only use of the DHA track: snapshot it, never write it.
	cert = frappe.get_doc("HISI DHA Certification Reference", case.dha_certificate)
	dha_status = dha_track(cert)

	if disposition in ("Validation passed", "Passed with findings"):
		if dha_status not in ("Verified", "Conditional"):
			frappe.throw(
				_("A passing SHA disposition requires an active DHA certification (current: {0}).").format(
					dha_status
				)
			)
		unassessed = [
			r.criterion_id
			for r in case.criteria_assessments
			if (r.assessment or "Not assessed") == "Not assessed"
		]
		if unassessed:
			frappe.throw(
				_("Assess every criterion before issuing a passing disposition: {0}").format(
					", ".join(unassessed)
				)
			)
		blockers = [
			r.criterion_id
			for r in case.criteria_assessments
			if r.is_critical and r.assessment not in PASSING_OUTCOMES
		]
		if blockers:
			frappe.throw(_("Mandatory criteria have not passed: {0}").format(", ".join(blockers)))
		open_findings = [f for f in _finding_rows(case.name) if f.status != "Resolved"]
		if any(f.severity == "Critical" for f in open_findings):
			frappe.throw(_("Resolve all critical findings before issuing a passing disposition."))
		if disposition == "Validation passed":
			if open_findings or any(r.assessment in ADVERSE_OUTCOMES for r in case.criteria_assessments):
				frappe.throw(
					_(
						"Open findings or adverse assessments remain; use 'Passed with findings' with conditions."
					)
				)
		elif not conditions:
			frappe.throw(_("State the conditions attached to a 'Passed with findings' disposition."))

	issued_on = now_datetime()
	bundle = _evidence_bundle(case, dha_status)
	bundle.update(
		{
			"disposition": disposition,
			"rationale": rationale,
			"conditions": conditions,
			"issuedBy": user,
			"issuedOn": fmt_dt(issued_on),
		}
	)
	bundle_hash = _canonical_hash(bundle)

	record = frappe.get_doc(
		{
			"doctype": "HISI SHA Disposition",
			"validation_case": case.name,
			"vendor": case.vendor,
			"disposition": disposition,
			"issued_by": user,
			"issued_on": issued_on,
			"dha_certificate": cert.name,
			"dha_status_at_issue": dha_status,
			"evidence_bundle_hash": bundle_hash,
			"rationale": rationale,
			"conditions": conditions,
			"evidence_bundle": json.dumps(bundle, sort_keys=True, indent=1, default=str),
		}
	).insert(ignore_permissions=True)

	case.update(
		{
			"state": DISPOSITION_TO_CASE_STATE[disposition],
			"sha_disposition": disposition,
			"disposition_record": record.name,
			"disposition_issued_on": issued_on,
			"disposition_issued_by": user,
			"next_action": None,
		}
	)
	case.save(ignore_permissions=True)
	return ok(
		{
			"issued": True,
			"bundleHash": bundle_hash,
			"dispositionId": record.name,
			"caseState": portal_state(case.state),
			"dhaStatus": dha_status,
		}
	)
