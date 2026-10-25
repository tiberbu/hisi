"""Permission-aware SHA validation workflow for the HISI evaluation workspace.

Backs ``/evaluation`` on the Next.js frontend: when a user signs in, their
organization's SHA validation case and confirmed DHA certificate are linked
automatically — the certificate recorded at sign-up (or verified against the
DHA register) appears on the evaluation page with no manual linking step.

The same module owns the vendor-side workflow:

- ``link_dha_certificate`` creates the organization's ``HISI SHA Validation
  Case`` linked to its confirmed certificate (one active workflow per
  organization);
- ``start_test_run`` queues the autonomous patient journey run as a
  ``HISI Test Run`` and moves the case to ``Running``.

Every read and write is scoped to the caller's organization through the
row-level rules in ``hisi.permissions``; the browser never decides which
case it sees or which certificate it may link.
"""

import os

import frappe
from frappe.model.workflow import WorkflowTransitionError, apply_workflow, get_transitions

from hisi.checklist import (
	assessments_of,
	criteria_of,
	evidence_rule,
	is_done,
	progress_map,
	step_done,
	step_graph,
	steps_of,
	status_completion,
	sub_steps_of,
)
from hisi.utils import get_dha_verify_url

# The only role that defines the platform separation (kept local so this
# module has no import cycle with hisi.api.account).
ADMIN_ROLE = "System Manager"

# Certificate statuses that count as confirmed for automatic linking
# (same set as hisi.api.account, kept local to avoid an import cycle).
CONFIRMED_CERTIFICATE_STATUSES = ("Valid", "Conditional")

# `HISI DHA Certification Reference.certificate_status` -> frontend badge.
CERTIFICATE_STATUS_LABELS = {
	"Valid": "Verified",
	"Conditional": "Conditional",
	"Expired": "Expired",
}
DEFAULT_CERTIFICATE_STATUS_LABEL = "Pending verification"

# Fallback DHA portal links when no register base URL is configured.
DEFAULT_DHA_TRACKER_URL = "https://certification.dha.go.ke/track"
DEFAULT_DHA_VERIFY_URL = "https://certification.dha.go.ke/verify"

# `HISI Organization Member.member_role` -> workflow display owner (same map
# as hisi.api.account, kept local to avoid an import cycle).
MEMBER_ROLE_MAP = {
	"Vendor Administrator": "HMIS Vendor Administrator",
	"Vendor Technical Lead": "Vendor Technical Lead",
	"Vendor Clinical / Operations Lead": "Vendor Clinical / Operations Lead",
}
DEFAULT_MEMBER_DISPLAY_ROLE = "HMIS Vendor Administrator"

# New cases insert at the workflow baseline; link_dha_certificate then
# advances them through the engine to Scope confirmation (states and
# transitions live in the "HISI SHA Validation Case Workflow" DocType).
WORKFLOW_INITIAL_STATE = "Linked to DHA record"
INITIAL_NEXT_ACTION = "Confirm scope and start technical checks"
RUNNING_NEXT_ACTION = "Review autonomous run results"
# Engine actions applied by the link flow, in order, after the insert.
LINK_CHAIN_ACTIONS = ("Verify DHA certificate", "Confirm SHA scope")
# Engine action that queues an autonomous run from any pre-test state.
START_RUN_ACTION = "Start test run"


# Assessment values that count as a positive criterion outcome (same set as
# hisi.api.admin, kept local to avoid an import cycle).
PASS_ASSESSMENTS = ("Pass", "Pass with observation")
MAX_STEP_NOTE_LENGTH = 280

# Case states where the checklist is finished: steps can no longer move.
TERMINAL_CASE_STATES = (
	"Validation passed",
	"Validation passed with findings",
	"Not validated",
	"Closed",
)

# Evidence files are restricted to documents and common image formats.
EVIDENCE_FILE_EXTENSIONS = (
	".jpg", ".jpeg", ".png", ".gif", ".pdf", ".txt", ".csv",
	".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)



def _session_user() -> str:
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Not authorized", frappe.PermissionError)
	return user


def _member_orgs(user: str) -> list[str]:
	return frappe.get_all("HISI Organization Member", filters={"user": user}, pluck="parent")


def _resolve_case(user: str, case_id: str | None):
	"""The validation case to render.

	An explicit id is permission-checked against the caller's organization;
	otherwise the most recently modified case of the caller's organization is
	used. Platform users without a membership see the most recent case
	overall. Returns ``None`` when there is no case to show (the frontend
	keeps its demo fallback), including for an unknown explicit id.
	"""
	if case_id:
		if not frappe.db.exists("HISI SHA Validation Case", case_id):
			return None
		doc = frappe.get_doc("HISI SHA Validation Case", case_id)
		doc.check_permission("read")
		return doc

	filters: dict = {"docstatus": ["<", 2]}
	orgs = _member_orgs(user)
	if orgs:
		filters["vendor"] = ["in", orgs]
	elif ADMIN_ROLE not in frappe.get_roles(user):
		return None

	cases = frappe.get_list(
		"HISI SHA Validation Case",
		filters=filters,
		fields=["name"],
		order_by="modified desc",
		limit_page_length=1,
	)
	if not cases:
		return None
	doc = frappe.get_doc("HISI SHA Validation Case", cases[0].name)
	doc.check_permission("read")
	return doc


def _dha_verify_base() -> str:
	"""DHA register base URL from HISI Settings, else the portal fallback."""
	return get_dha_verify_url() or DEFAULT_DHA_VERIFY_URL


def _dha_certificate_payload(certificate_id: str | None) -> dict:
	"""Frontend ``DhaCertificateRecord`` for a certification reference."""
	if not certificate_id:
		return {}
	cert = frappe.get_doc("HISI DHA Certification Reference", certificate_id)
	cert.check_permission("read")
	vendor_name = frappe.db.get_value("HISI Vendor Organization", cert.vendor, "organization_name") or cert.vendor
	system_name = frappe.db.get_value("HISI HMIS Product", cert.product, "product_name") or cert.product
	status = cert.certificate_status or "Unknown"
	return {
		"certificateId": cert.certificate_id,
		"systemName": system_name,
		"vendorName": vendor_name,
		"scopeType": f"First-round DHA HMIS Certification ({status})",
		"dhaStatus": CERTIFICATE_STATUS_LABELS.get(status, DEFAULT_CERTIFICATE_STATUS_LABEL),
		"issueDate": str(cert.issue_date) if cert.issue_date else "",
		"expiryDate": str(cert.expiry_date) if cert.expiry_date else "",
		"verifiedAt": str(cert.verified_on)[:10] if cert.verified_on else "",
		"verifierSource": cert.verified_by or "DHA public register",
		"trackerUrl": DEFAULT_DHA_TRACKER_URL,
		"verifyUrl": f"{_dha_verify_base()}/{cert.certificate_id}",
	}


def _organization_certificate_payload(user: str) -> dict:
	"""The caller's organization's confirmed certificate, most recently verified first.

	Used when the organization has no SHA validation case yet, so /evaluation
	links the real DHA certificate instead of keeping the demo fallback.
	"""
	orgs = _member_orgs(user)
	if not orgs:
		return {}
	certificates = frappe.get_all(
		"HISI DHA Certification Reference",
		filters={
			"vendor": ["in", orgs],
			"certificate_status": ["in", list(CONFIRMED_CERTIFICATE_STATUSES)],
		},
		fields=["certificate_id"],
		order_by="verified_on desc, modified desc",
		limit_page_length=1,
	)
	if not certificates:
		return {}
	return _dha_certificate_payload(certificates[0].certificate_id)


def _evidence_mode(evidence_options: str | None) -> str:
	"""Derive the frontend evidence-mode badge from the checklist wording."""
	text = (evidence_options or "").lower()
	has_api = "api" in text
	has_manual = any(token in text for token in ("video", "manual", "plan", "record", "report", "evidence"))
	if has_api and has_manual:
		return "Mixed"
	if has_api:
		return "API"
	if has_manual:
		return "Manual/video"
	return "Mixed"


def _sub_step_payload(step, sub_step, row) -> dict:
	return {
		"subStepKey": sub_step.sub_step_key,
		"label": sub_step.label or "",
		"description": sub_step.description or "",
		"sortOrder": int(sub_step.sort_order or 0),
		"done": is_done(row),
		"note": (row.note if row else "") or "",
		"evidenceFile": (row.evidence_file if row else "") or "",
		"completedBy": (row.completed_by if row else "") or "",
		"completedOn": str(row.completed_on)[:16] if row and row.completed_on else "",
		"reopened": bool(row and row.reopened),
		"evidenceRequired": bool(sub_step.evidence_required),
		"evidenceType": sub_step.evidence_type or "Note",
		"evidenceHint": sub_step.evidence_hint or "",
	}


def _completion_summary(rows: list) -> dict:
	"""Note/file/actor of the most recently completed row of one step."""
	done = [row for row in rows if is_done(row)]
	latest = max(done, key=lambda row: str(row.completed_on or "")) if done else None
	return {
		"note": (latest.note if latest else "") or "",
		"evidenceFile": (latest.evidence_file if latest else "") or "",
		"completedBy": (latest.completed_by if latest else "") or "",
		"completedOn": str(latest.completed_on)[:16] if latest and latest.completed_on else "",
	}


def _criteria_payload(doc) -> list[dict]:
	"""Checklist criteria with this case's assessments and step progress, frontend-shaped.

	Steps are flat documents now: ``currentStep`` is derived (first step that
	is not complete) instead of being stored on the assessment.
	"""
	if not doc.checklist_version:
		return []
	checklist = frappe.get_doc("HISI Checklist Version", doc.checklist_version)
	checklist.check_permission("read")

	by_criterion, sub_steps_by_step = step_graph(doc.checklist_version)
	assessment_by_id = assessments_of(doc.name)
	progress = progress_map(case_name=doc.name)

	criteria = []
	for criterion in criteria_of(doc.checklist_version):
		assessment = assessment_by_id.get(criterion.criterion_id)
		steps = by_criterion.get(criterion.criterion_id, [])
		step_payloads = []
		current_step = ""
		completed_keys: set[str] = set()
		for step in steps:
			sub_list = sub_steps_by_step.get(step.name, [])
			done = step_done(step, sub_list, progress)
			if done:
				completed_keys.add(step.step_key)
			elif not current_step:
				current_step = step.step_key
			direct_row = progress.get((step.name, ""))
			sub_rows = [progress.get((step.name, sub.name)) for sub in sub_list]
			summary = _completion_summary([direct_row] if not sub_list else sub_rows)
			reopened = (
				any(row and row.reopened for row in sub_rows)
				if sub_list
				else bool(direct_row and direct_row.reopened)
			)
			step_payloads.append(
				{
					"stepKey": step.step_key,
					"label": step.label or "",
					"description": step.description or "",
					"sortOrder": int(step.sort_order or 0),
					"requiresNote": bool(step.requires_note),
					"checklistStatus": step.checklist_status,
					"evidenceRequired": bool(step.evidence_required),
					"evidenceType": step.evidence_type or "Note",
					"evidenceHint": step.evidence_hint or "",
					"done": done,
					"reopened": reopened,
					**summary,
					"subSteps": [
						_sub_step_payload(
							step, sub, progress.get((step.name, sub.name))
						)
						for sub in sub_list
					],
				}
			)
		if steps:
			# A criterion with defined steps is accomplished only when every
			# step is complete.
			accomplished = len(completed_keys) == len(steps)
		else:
			accomplished = bool(assessment) and assessment.assessment in PASS_ASSESSMENTS
		criteria.append(
			{
				"id": criterion.criterion_id,
				"title": criterion.title,
				"coverage": criterion.statement or "",
				"evidenceMode": _evidence_mode(criterion.evidence_options),
				"mandatoryBlocker": bool(criterion.is_critical),
				"assessment": assessment.assessment if assessment else "Not assessed",
				"automatedStatus": "Waiting",
				"manualEvidenceCount": 0,
				"openFindingsCount": 0,
				"owner": doc.current_owner or "SHA Validation Reviewer",
				"rationale": (assessment.rationale if assessment else None) or "",
				"steps": step_payloads,
				"currentStep": current_step,
				"isAccomplished": accomplished,
			}
		)
	return criteria


def _status_progress_payload(doc) -> list[dict]:
	"""Per checklist status step completion for this case, in status order."""
	completion = status_completion(doc.name, doc.checklist_version)
	return [
		{
			"statusKey": bucket["statusKey"],
			"label": bucket["label"],
			"required": bucket["required"],
			"completed": bucket["completed"],
			"complete": bucket["complete"],
			"remaining": bucket["remaining"],
		}
		for bucket in sorted(
			(bucket for bucket in completion.values() if bucket["enabled"]),
			key=lambda bucket: (bucket["sortOrder"], bucket["statusKey"]),
		)
	]


def _member_display_role(user: str) -> str:
	"""The workspace display role used as the workflow's current owner."""
	membership = frappe.get_all(
		"HISI Organization Member",
		filters={"user": user},
		fields=["member_role"],
		limit_page_length=1,
	)
	if not membership:
		return DEFAULT_MEMBER_DISPLAY_ROLE
	return MEMBER_ROLE_MAP.get(membership[0].member_role) or DEFAULT_MEMBER_DISPLAY_ROLE


def _next_sequential_name(doctype: str, field: str, prefix: str) -> str:
	"""Next unused ``{prefix}0001``-style name for an autoname-by-field doctype."""
	used = set(frappe.get_all(doctype, filters=[[field, "like", f"{prefix}%"]], pluck=field))
	sequence = len(used) + 1
	while f"{prefix}{sequence:04d}" in used:
		sequence += 1
	return f"{prefix}{sequence:04d}"


def _latest_checklist_version() -> str:
	"""Newest checklist a case may be created against: published and enabled
	first, then any published one (so a site with everything disabled keeps
	working), and only then a clear failure."""
	for filters in ({"published": 1, "enabled": 1}, {"published": 1}):
		checklist = frappe.get_all(
			"HISI Checklist Version",
			filters=filters,
			fields=["name"],
			order_by="effective_on desc, modified desc",
			limit_page_length=1,
		)
		if checklist:
			return checklist[0].name
	frappe.throw("No published SHA checklist is available.", frappe.ValidationError)


def _find_certificate(certificate_id: str):
	certificates = frappe.get_all(
		"HISI DHA Certification Reference",
		or_filters=[
			["certificate_id", "=", certificate_id],
			["name", "=", certificate_id],
		],
		fields=["name", "vendor", "product", "certificate_status"],
		limit_page_length=1,
	)
	return certificates[0] if certificates else None


def _available_actions(doc) -> list[dict]:
	"""Workflow actions the current user may apply to the case right now.

	Derived from the engine's transition matrix filtered by role, so the
	frontend only ever sees buttons the caller is allowed to press.
	"""
	transitions = get_transitions(doc)
	seen: set[tuple[str, str]] = set()
	actions: list[dict] = []
	for transition in transitions:
		key = (transition["action"], transition["next_state"])
		if key in seen:
			continue
		seen.add(key)
		actions.append({"action": transition["action"], "nextState": transition["next_state"]})
	return actions


def _next_action_href(available_actions: list[dict]) -> str:
	"""Where the evaluation "Proceed" action leads for this workflow."""
	if any(action["action"] in ("Start test run", "Start retest run") for action in available_actions):
		return "/certification/run"
	return "/certification"


@frappe.whitelist()
def link_dha_certificate(
	certificate_id: str,
	release_version: str,
	system_name: str = "",
	vendor_name: str = "",
	scope_notes: str = "",
) -> dict:
	"""Create the organization's SHA validation workflow, linked to its DHA certificate.

	Session-gated and organization-scoped: the certificate must belong to the
	caller's organization and be confirmed (Valid or Conditional) — the
	submitted system/vendor names are advisory only. Linking the same
	certificate again returns the existing workflow; a second workflow for
	the organization is rejected. Returns ``caseId`` so the frontend can
	track the workflow it just started.
	"""
	user = _session_user()
	orgs = _member_orgs(user)
	if not orgs:
		frappe.throw("You are not linked to a vendor organization.", frappe.PermissionError)

	certificate_id = (certificate_id or "").strip()
	release_version = (release_version or "").strip()
	if not certificate_id:
		frappe.throw("certificate_id is required.", frappe.ValidationError)
	if not release_version:
		frappe.throw("release_version is required.", frappe.ValidationError)

	certificate = _find_certificate(certificate_id)
	if not certificate:
		frappe.throw("DHA certificate not found.", frappe.ValidationError)
	if certificate.vendor not in orgs:
		frappe.throw("This certificate belongs to another organization.", frappe.PermissionError)
	if certificate.certificate_status not in CONFIRMED_CERTIFICATE_STATUSES:
		frappe.throw(
			f"Certificate is not verified against the DHA register "
			f"(status: {certificate.certificate_status or 'unknown'}).",
			frappe.ValidationError,
		)

	existing = frappe.get_list(
		"HISI SHA Validation Case",
		filters={"vendor": certificate.vendor, "docstatus": ["<", 2]},
		fields=["name", "dha_certificate"],
		order_by="modified desc",
		limit_page_length=1,
	)
	if existing:
		if existing[0].dha_certificate == certificate.name:
			return {"caseId": existing[0].name, "status": "already linked"}
		frappe.throw(
			f"Your organization already has an SHA validation workflow: {existing[0].name}.",
			frappe.DuplicateEntryError,
		)

	case_name = _next_sequential_name(
		"HISI SHA Validation Case", "case_name", f"SHA-VAL-{frappe.utils.nowdate()[:4]}-"
	)
	frappe.db.savepoint("validation_link_certificate")
	try:
		case = frappe.get_doc(
			{
				"doctype": "HISI SHA Validation Case",
				"case_name": case_name,
				"vendor": certificate.vendor,
				"product": certificate.product,
				"dha_certificate": certificate.name,
				"checklist_version": _latest_checklist_version(),
				"release_version": release_version,
				"scope": "SHA Digital Claims workflow",
				"scope_notes": (scope_notes or "").strip(),
				"state": WORKFLOW_INITIAL_STATE,
				"current_owner": _member_display_role(user),
				"next_action": INITIAL_NEXT_ACTION,
			}
		).insert()
		# The certificate was verified against the register above, so the
		# engine records both the verification and the scope confirmation
		# as real transitions (with actor + timestamp comments).
		for action in LINK_CHAIN_ACTIONS:
			case = apply_workflow(case, action)
	except Exception:
		frappe.db.rollback(save_point="validation_link_certificate")
		raise

	frappe.db.commit()
	return {"caseId": case.name, "status": "created"}


@frappe.whitelist()
def start_test_run(case_id: str, environment_url: str, synthetic_pack: str | None = None) -> dict:
	"""Queue the autonomous patient journey run for an SHA validation case.

	Session-gated and organization-scoped through ``_resolve_case``: only a
	case of the caller's own organization (or any case for platform users)
	can be started. The run is recorded as a ``HISI Test Run`` and the case
	moves to ``Running`` with its next action set to reviewing results.
	"""
	user = _session_user()
	case = _resolve_case(user, case_id)
	if not case:
		frappe.throw(
			"SHA validation case not found. Link your DHA certificate first.",
			frappe.DoesNotExistError,
		)
	# The run stays bound to step 1: re-check the linked certificate is still
	# confirmed at run start, not only at link time.
	if case.dha_certificate:
		certificate = _find_certificate(case.dha_certificate)
		if not certificate or certificate.certificate_status not in CONFIRMED_CERTIFICATE_STATUSES:
			frappe.throw(
				f"Linked DHA certificate {case.dha_certificate} is no longer confirmed"
				f" (status: {certificate.certificate_status if certificate else 'missing'})."
				" Restore certification before starting a run.",
				frappe.ValidationError,
			)
	environment_url = (environment_url or "").strip()
	if not environment_url:
		frappe.throw("environment_url is required.", frappe.ValidationError)

	run_name = _next_sequential_name("HISI Test Run", "run_name", f"RUN-{frappe.utils.nowdate()[:4]}-")
	frappe.db.savepoint("validation_start_run")
	try:
		# The engine validates the transition (state + role + self-approval)
		# and records the move to Running with an audit comment.
		try:
			case = apply_workflow(case, START_RUN_ACTION)
		except WorkflowTransitionError:
			frappe.throw(
				f"Case {case.name} is closed for new runs (state: {case.state}).",
				frappe.ValidationError,
			)
		case.next_action = RUNNING_NEXT_ACTION
		case.save()
		run = frappe.get_doc(
			{
				"doctype": "HISI Test Run",
				"run_name": run_name,
				"case": case.name,
				"vendor": case.vendor,
				"environment_url": environment_url,
				"synthetic_pack": (synthetic_pack or "").strip(),
				"status": "Queued",
				"started_on": frappe.utils.now_datetime(),
				"started_by": user,
			}
		).insert()
	except Exception:
		frappe.db.rollback(save_point="validation_start_run")
		raise

	frappe.db.commit()
	return {"runId": run.name, "status": "queued"}


@frappe.whitelist()
def get_case_details(case_id: str | None = None) -> dict:
	"""The signed-in user's validation case with its DHA certificate linked.

	Without a case the organization's confirmed certificate is still returned
	so /evaluation shows the real record; the frontend keeps its demo
	fallback only for the case fields.
	"""
	user = _session_user()
	doc = _resolve_case(user, case_id)
	if not doc:
		return {"caseLinked": False, "dhaCertificate": _organization_certificate_payload(user)}
	available_actions = _available_actions(doc)
	return {
		"caseLinked": True,
		"caseId": doc.name,
		"releaseVersion": doc.release_version or "",
		"checklistVersion": doc.checklist_version or "",
		"shaState": doc.state or "",
		"responsibleOwner": doc.current_owner or "",
		"nextActionLabel": doc.next_action or "",
		"nextActionHref": _next_action_href(available_actions),
		"availableActions": available_actions,
		"dhaCertificate": _dha_certificate_payload(doc.dha_certificate),
		"criteria": _criteria_payload(doc),
		"statusProgress": _status_progress_payload(doc),
	}


def _resolve_step(case, criterion_id: str, step_key: str, sub_step_key: str):
	"""``(criterion, step, sub_step)`` of the case's checklist, or throws.

	``sub_step`` is ``None`` only when the step has no sub steps at all.
	"""
	if not case.checklist_version:
		frappe.throw("This case has no checklist linked.", frappe.ValidationError)
	criterion = next(
		(c for c in criteria_of(case.checklist_version) if c.criterion_id == criterion_id),
		None,
	)
	if criterion is None:
		frappe.throw(
			f"Criterion '{criterion_id}' is not part of checklist {case.checklist_version}.",
			frappe.ValidationError,
		)
	step = next(
		(s for s in steps_of(case.checklist_version, criterion_id) if s.step_key == step_key),
		None,
	)
	if step is None:
		frappe.throw(
			f"Step '{step_key}' is not part of criterion '{criterion_id}'.",
			frappe.ValidationError,
		)
	sub_steps = sub_steps_of([step.name]).get(step.name, [])
	sub_step = None
	if sub_step_key:
		sub_step = next((s for s in sub_steps if s.sub_step_key == sub_step_key), None)
		if sub_step is None:
			frappe.throw(
				f"Sub step '{sub_step_key}' is not part of step '{step_key}'.",
				frappe.ValidationError,
			)
	elif sub_steps:
		frappe.throw(
			f"Step '{step.label or step_key}' is made of sub steps "
			f"({', '.join(s.sub_step_key for s in sub_steps)}); complete those instead.",
			frappe.ValidationError,
		)
	return criterion, step, sub_step


def _unit_label(step, sub_step) -> str:
	return ((sub_step.label if sub_step else step.label) or (sub_step.sub_step_key if sub_step else step.step_key))


def _assessment_for(case, criterion) -> str:
	"""Name of the case's assessment row for one criterion (created on demand)."""
	existing = frappe.db.get_value(
		"HISI Case Criterion Assessment",
		{"case": case.name, "criterion_id": criterion.criterion_id},
		"name",
	)
	if existing:
		return existing
	return (
		frappe.get_doc(
			{
				"doctype": "HISI Case Criterion Assessment",
				"case": case.name,
				"criterion_id": criterion.criterion_id,
				"criterion_title": criterion.title,
				"is_critical": int(criterion.is_critical or 0),
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _progress_row(assessment_name: str, step, sub_step):
	"""The stored progress row of one (assessment, step, sub step) unit, if any.

	The sub step column is nullable, so the empty spelling is compared in
	Python rather than in the filter.
	"""
	target = sub_step.name if sub_step else ""
	for row in frappe.get_all(
		"HISI Case Criterion Step Progress",
		filters={"assessment": assessment_name, "step": step.name},
		fields=["name", "sub_step"],
	):
		if (row.sub_step or "") == target:
			return frappe.get_doc("HISI Case Criterion Step Progress", row.name)
	return None


def _check_evidence(step, sub_step, note: str, evidence_file: str) -> None:
	"""Block completion until the configured evidence is supplied."""
	required, evidence_type, _hint = evidence_rule(step, sub_step)
	label = _unit_label(step, sub_step)
	if required and evidence_type == "File" and not evidence_file:
		frappe.throw(f"An evidence file is required to complete '{label}'.", frappe.ValidationError)
	if required and evidence_type == "Note" and not note:
		frappe.throw(
			f"A note or evidence citation is required to complete '{label}'.",
			frappe.ValidationError,
		)
	if required and evidence_type == "Note or file" and not (note or evidence_file):
		frappe.throw(
			f"A note or evidence file is required to complete '{label}'.",
			frappe.ValidationError,
		)
	if not required and sub_step is None and step.requires_note and not note:
		frappe.throw(
			f"A note or evidence citation is required to complete '{label}'.",
			frappe.ValidationError,
		)


def _attach_evidence_file(user: str, file_name: str, progress_name: str) -> str:
	"""Attach an uploaded ``File`` to a progress row and return its name.

	Uploads go through Frappe's built-in ``POST /api/method/upload_file``
	(no doctype => the file is owned by the caller); this only validates
	ownership, format and that the file is not already attached elsewhere.
	"""
	file = frappe.db.get_value(
		"File",
		(file_name or "").strip(),
		["name", "file_name", "owner", "attached_to_doctype", "attached_to_name"],
		as_dict=True,
	)
	if not file:
		frappe.throw(
			"Evidence file not found; upload it with /api/method/upload_file first.",
			frappe.ValidationError,
		)
	if file.owner != user and ADMIN_ROLE not in frappe.get_roles(user):
		frappe.throw("This evidence file belongs to another user.", frappe.PermissionError)
	attached_elsewhere = file.attached_to_doctype and (
		file.attached_to_doctype != "HISI Case Criterion Step Progress"
		or file.attached_to_name != progress_name
	)
	if attached_elsewhere:
		frappe.throw(
			"This evidence file is already attached to another record.",
			frappe.ValidationError,
		)
	extension = (os.path.splitext(file.file_name or file.name or "")[1] or "").lower()
	if extension and extension not in EVIDENCE_FILE_EXTENSIONS:
		frappe.throw(
			f"Files of type '{extension}' are not accepted as evidence "
			f"(allowed: {', '.join(EVIDENCE_FILE_EXTENSIONS)}).",
			frappe.ValidationError,
		)
	frappe.db.set_value(
		"File",
		file.name,
		{
			"attached_to_doctype": "HISI Case Criterion Step Progress",
			"attached_to_name": progress_name,
			"attached_to_field": "evidence_file",
		},
	)
	return file.name


def _criterion_progress(case, criterion_id: str) -> dict:
	"""``accomplished``/``currentStep``/``remainingSteps`` for one criterion."""
	progress = progress_map(case_name=case.name)
	by_criterion, sub_steps_by_step = step_graph(case.checklist_version)
	open_keys = [
		step.step_key
		for step in by_criterion.get(criterion_id, [])
		if not step_done(step, sub_steps_by_step.get(step.name, []), progress)
	]
	return {
		"accomplished": bool(by_criterion.get(criterion_id)) and not open_keys,
		"currentStep": open_keys[0] if open_keys else "",
		"remainingSteps": open_keys,
	}


@frappe.whitelist()
def complete_criterion_step(
	case_id: str,
	criterion_id: str,
	step_key: str = "",
	sub_step_key: str = "",
	note: str = "",
	evidence_file: str = "",
) -> dict:
	"""Record the vendor's completion of one step (or sub step).

	Session-gated and organization-scoped through ``_resolve_case``. The unit
	must supply the evidence its checklist config demands (a text citation
	and/or a file uploaded through ``/api/method/upload_file``). Progress is
	stored per sub step; a step without sub steps completes directly.
	"""
	user = _session_user()
	case = _resolve_case(user, (case_id or "").strip() or None)
	if not case:
		frappe.throw(
			"SHA validation case not found. Link your DHA certificate first.",
			frappe.DoesNotExistError,
		)
	criterion_id = (criterion_id or "").strip()
	step_key = (step_key or "").strip().lower()
	sub_step_key = (sub_step_key or "").strip().lower()
	note = (note or "").strip()[:MAX_STEP_NOTE_LENGTH]
	uploaded = (evidence_file or "").strip()
	if not criterion_id:
		frappe.throw("criterion_id is required.", frappe.ValidationError)
	if not step_key:
		frappe.throw("step_key is required.", frappe.ValidationError)

	criterion, step, sub_step = _resolve_step(case, criterion_id, step_key, sub_step_key)
	_check_evidence(step, sub_step, note, uploaded)

	assessment_name = _assessment_for(case, criterion)
	row = _progress_row(assessment_name, step, sub_step)
	if row and is_done(row):
		frappe.throw(
			f"'{_unit_label(step, sub_step)}' was already completed.",
			frappe.ValidationError,
		)

	if row:
		# Re-completing after a re-open: same row, fresh actor and timestamp,
		# the re-open audit fields stay for the record.
		row.note = note
		row.reopened = 0
		row.completed_by = user
		row.completed_on = frappe.utils.now()
		row.save(ignore_permissions=True)
	else:
		row = frappe.get_doc(
			{
				"doctype": "HISI Case Criterion Step Progress",
				"case": case.name,
				"assessment": assessment_name,
				"step": step.name,
				"sub_step": sub_step.name if sub_step else "",
				"step_key": step.step_key,
				"sub_step_key": sub_step.sub_step_key if sub_step else "",
				"step_label": step.label or step.step_key,
				"note": note,
				"completed_by": user,
				"completed_on": frappe.utils.now(),
			}
		).insert(ignore_permissions=True)

	if uploaded:
		attached = _attach_evidence_file(user, uploaded, row.name)
		if row.evidence_file and row.evidence_file != attached:
			frappe.db.set_value(
				"File", row.evidence_file, {"attached_to_doctype": None, "attached_to_name": None}
			)
		if row.evidence_file != attached:
			row.evidence_file = attached
			row.save(ignore_permissions=True)

	frappe.db.commit()

	progress = _criterion_progress(case, criterion_id)
	return {
		"caseId": case.name,
		"criterionId": criterion_id,
		"completedStep": step_key,
		"completedSubStep": sub_step.sub_step_key if sub_step else "",
		"accomplished": progress["accomplished"],
		"currentStep": progress["currentStep"],
		"remainingSteps": progress["remainingSteps"],
		"statusProgress": _status_progress_payload(case),
	}


@frappe.whitelist()
def reopen_criterion_step(
	case_id: str,
	criterion_id: str,
	step_key: str = "",
	sub_step_key: str = "",
	reason: str = "",
) -> dict:
	"""Send a completed step (or sub step) back to the vendor.

	Session-gated and organization-scoped through ``_resolve_case``; blocked
	once the case is in a terminal state. The row is kept with who re-opened
	it and why, and the next completion clears the flag again.
	"""
	user = _session_user()
	case = _resolve_case(user, (case_id or "").strip() or None)
	if not case:
		frappe.throw(
			"SHA validation case not found. Link your DHA certificate first.",
			frappe.DoesNotExistError,
		)
	if case.state in TERMINAL_CASE_STATES:
		frappe.throw(
			f"Case {case.name} is closed ({case.state}); steps can no longer be re-opened.",
			frappe.ValidationError,
		)
	criterion_id = (criterion_id or "").strip()
	step_key = (step_key or "").strip().lower()
	sub_step_key = (sub_step_key or "").strip().lower()
	if not criterion_id:
		frappe.throw("criterion_id is required.", frappe.ValidationError)
	if not step_key:
		frappe.throw("step_key is required.", frappe.ValidationError)

	_criterion, step, sub_step = _resolve_step(case, criterion_id, step_key, sub_step_key)
	assessment_name = frappe.db.get_value(
		"HISI Case Criterion Assessment",
		{"case": case.name, "criterion_id": criterion_id},
		"name",
	)
	row = _progress_row(assessment_name, step, sub_step) if assessment_name else None
	if not row:
		frappe.throw(
			f"'{_unit_label(step, sub_step)}' has not been completed yet.",
			frappe.ValidationError,
		)
	if not row.reopened:
		row.reopened = 1
		row.reopened_by = user
		row.reopened_on = frappe.utils.now()
		row.reopen_reason = (reason or "").strip()[:MAX_STEP_NOTE_LENGTH]
		row.save(ignore_permissions=True)
		frappe.db.commit()

	progress = _criterion_progress(case, criterion_id)
	return {
		"caseId": case.name,
		"criterionId": criterion_id,
		"reopenedStep": step_key,
		"reopenedSubStep": sub_step.sub_step_key if sub_step else "",
		"accomplished": progress["accomplished"],
		"currentStep": progress["currentStep"],
		"remainingSteps": progress["remainingSteps"],
		"statusProgress": _status_progress_payload(case),
	}
