"""Canonical HISI roles, reference data and state vocabulary.

The SHA claims validation reference data (7 certification criteria, the
10-stage patient journey and the fraud-control traceability matrix) lives in
``hisi/data/sha_reference.json`` and mirrors ``lib/hisi-types.ts`` in the
HISI portal (docs/BRD-REQUIREMENTS.md). Keep both in sync.
"""

import json
import os
from functools import lru_cache

# ---------------------------------------------------------------- roles

VENDOR_ADMIN = "HMIS Vendor Administrator"
VENDOR_TECH = "Vendor Technical Lead"
VENDOR_CLINICAL = "Vendor Clinical / Operations Lead"

SHA_REVIEWER = "SHA Validation Reviewer"
TECH_REVIEWER = "Technical/API Reviewer"
CLINICAL_REVIEWER = "Clinical/Claims Reviewer"
DECISION_AUTHORITY = "SHA Validation Decision Authority"
HISI_ADMIN = "HISI Administrator"
AUDITOR = "Auditor / Observer"

# Legacy roles from the first vertical slice; still honoured.
LEGACY_REVIEWER = "HISI Reviewer"
LEGACY_VENDOR = "HISI Vendor"

VENDOR_ROLES = (VENDOR_ADMIN, VENDOR_TECH, VENDOR_CLINICAL, LEGACY_VENDOR)

# Roles allowed to assess criteria and issue SHA dispositions (HISI-FR-006 handoff).
REVIEWER_ROLES = (
	SHA_REVIEWER,
	TECH_REVIEWER,
	CLINICAL_REVIEWER,
	DECISION_AUTHORITY,
	HISI_ADMIN,
	LEGACY_REVIEWER,
)

# Roles that can read across every vendor organization (no tenant scoping).
CROSS_ORG_READ_ROLES = (*REVIEWER_ROLES, AUDITOR, "System Manager")

# Display priority used to pick the single "role" shown on the profile.
ROLE_PRIORITY = (
	HISI_ADMIN,
	DECISION_AUTHORITY,
	SHA_REVIEWER,
	TECH_REVIEWER,
	CLINICAL_REVIEWER,
	AUDITOR,
	VENDOR_ADMIN,
	VENDOR_TECH,
	VENDOR_CLINICAL,
)

# Roles created by after_install / after_migrate. Portal roles have no Desk access.
ROLE_DEFINITIONS = {
	VENDOR_ADMIN: 0,
	VENDOR_TECH: 0,
	VENDOR_CLINICAL: 0,
	SHA_REVIEWER: 0,
	TECH_REVIEWER: 0,
	CLINICAL_REVIEWER: 0,
	DECISION_AUTHORITY: 0,
	AUDITOR: 0,
	HISI_ADMIN: 1,
	LEGACY_REVIEWER: 0,
	LEGACY_VENDOR: 0,
}

# Org-member role label (HISI Organization Member.member_role) -> Frappe role.
MEMBER_ROLE_TO_ROLE = {
	"Vendor Administrator": VENDOR_ADMIN,
	"Vendor Technical Lead": VENDOR_TECH,
	"Vendor Clinical / Operations Lead": VENDOR_CLINICAL,
}

# Self-registration role keys accepted from the portal sign-up form.
SIGNUP_ROLE_KEYS = {
	"vendor_admin": "Vendor Administrator",
	"vendor_tech": "Vendor Technical Lead",
}

# ---------------------------------------------------------------- vocabularies

CRITERION_OUTCOMES = (
	"Not assessed",
	"Not applicable",
	"Pass",
	"Pass with observation",
	"Partial",
	"Fail",
	"Blocked",
)
PASSING_OUTCOMES = ("Pass", "Pass with observation", "Not applicable")
ADVERSE_OUTCOMES = ("Partial", "Fail", "Blocked")

DISPOSITIONS = (
	"Validation passed",
	"Passed with findings",
	"Remediation required",
	"Not validated",
	"Blocked",
)

# Portal disposition -> stored HISI SHA Validation Case.state value.
DISPOSITION_TO_CASE_STATE = {
	"Validation passed": "Validation passed",
	"Passed with findings": "Validation passed with findings",
	"Remediation required": "Remediation required",
	"Not validated": "Not validated",
	"Blocked": "Blocked",
}

# Stored case state -> portal ValidationCaseState (only where names differ).
CASE_STATE_TO_PORTAL = {
	"Validation passed with findings": "Passed with findings",
	"Surveillance active": "Surveillance",
	"Closed": "Not validated",
}

# States from which an autonomous run may be started.
RUNNABLE_STATES = (
	"Linked to DHA record",
	"Certificate verified",
	"Scope confirmation",
	"Test plan review",
	"Environment provisioning",
	"Ready to test",
	"Results available",
	"Manual review",
	"Remediation required",
	"Retest requested",
)
FINAL_STATES = (
	"Validation passed",
	"Validation passed with findings",
	"Not validated",
	"Surveillance active",
	"Closed",
)

# DHA certificate_status (stored) -> portal dhaStatus track.
DHA_STATUS_TO_PORTAL = {
	"Valid": "Verified",
	"Pending verification": "Pending verification",
	"Conditional": "Conditional",
	"Expired": "Expired",
	"Suspended": "Pending verification",
	"Unknown": "Pending verification",
}

STEP_STATUSES = ("Waiting", "Running", "Pass", "Fail", "Blocked", "Skipped", "Needs reviewer")

NEXT_ACTIONS = {
	"Linked to DHA record": ("Confirm SHA scope and connect test environment", "/interoperability/connect"),
	"Certificate verification pending": (
		"Await DHA certificate verification",
		"/certification/link-certificate",
	),
	"Certificate verified": ("Connect test environment", "/interoperability/connect"),
	"Scope confirmation": ("Connect test environment and start technical checks", "/certification/run"),
	"Test plan review": ("Start autonomous journey checks", "/certification/run"),
	"Environment provisioning": ("Start autonomous journey checks", "/certification/run"),
	"Ready to test": ("Start autonomous journey checks", "/certification/run"),
	"Running": ("Monitor autonomous journey run", "/certification/run"),
	"Results available": ("Submit manual evidence and request review", "/certification/evidence/new"),
	"Manual review": ("Reviewer criterion assessment in progress", "/certification"),
	"Remediation required": ("Respond to open findings and retest", "/certification"),
	"Retest requested": ("Re-run affected journey stages", "/certification/run"),
	"SHA disposition review": ("Issue SHA claims validation disposition", "/certification/disposition"),
	"Validation passed": ("Enable continuous surveillance", "/surveillance"),
	"Validation passed with findings": ("Track conditions and enable surveillance", "/surveillance"),
	"Not validated": ("Review findings before re-applying", "/certification"),
	"Blocked": ("Resolve blocking issue with SHA", "/certification"),
	"Surveillance active": ("Review surveillance signals", "/surveillance"),
	"Closed": ("Case closed", "/certification"),
}


@lru_cache(maxsize=1)
def _reference() -> dict:
	path = os.path.join(os.path.dirname(__file__), "data", "sha_reference.json")
	with open(path) as fh:
		return json.load(fh)


def criteria_reference() -> list[dict]:
	return _reference()["criteria"]


def criterion_reference(criterion_id: str) -> dict | None:
	return next((c for c in criteria_reference() if c["id"] == criterion_id), None)


def journey_stages_reference() -> list[dict]:
	return _reference()["journeyStages"]


def stage_reference(code: str) -> dict | None:
	return next((s for s in journey_stages_reference() if s["code"] == code), None)


def fraud_controls_reference() -> list[dict]:
	return _reference()["fraudControls"]
