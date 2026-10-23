"""Create the Frappe workflow for HISI SHA Validation Case.

State vocabulary and lifecycle follow BRD-REQUIREMENTS.md section 4.1 and
the section 4 flowchart: vendors drive certification/scope/environment/run
states, HISI Reviewers drive review/retest/disposition states, and
transitions record actor plus timestamp through the engine's Workflow
comments. The workflow's state field is the existing ``state`` Select so
current data and UIs keep working.

Idempotent: workflow states and actions are upserted by name and the
workflow's child rows are rebuilt on every run.
"""

import frappe

WORKFLOW_NAME = "HISI SHA Validation Case Workflow"
DOCUMENT_TYPE = "HISI SHA Validation Case"
STATE_FIELD = "state"

VENDOR = "HISI Vendor"
REVIEWER = "HISI Reviewer"
PLATFORM = "System Manager"

# Insert baseline must stay first: new cases start here and the link flow
# advances them through the engine to Scope confirmation. Values are the
# role allowed to edit the document while it sits in that state.
STATES = {
	"Linked to DHA record": VENDOR,
	"Certificate verification pending": REVIEWER,
	"Certificate verified": VENDOR,
	"Scope confirmation": VENDOR,
	"Test plan review": REVIEWER,
	"Environment provisioning": VENDOR,
	"Ready to test": VENDOR,
	"Running": VENDOR,
	"Results available": VENDOR,
	"Manual review": REVIEWER,
	"Remediation required": VENDOR,
	"Retest requested": VENDOR,
	"SHA disposition review": REVIEWER,
	"Validation passed": REVIEWER,
	"Validation passed with findings": REVIEWER,
	"Not validated": REVIEWER,
	"Blocked": REVIEWER,
	"Surveillance active": REVIEWER,
	"Closed": REVIEWER,
}

# States the admin acknowledgement API accepts today (any state may be
# acknowledged once its criteria pass); "Validation passed" itself excluded.
ACKNOWLEDGE_FROM = [s for s in STATES if s != "Validation passed"]

# Starting a run directly from any pre-test state keeps today's Proceed ->
# runner flow working while the intermediate approval gates exist for the
# UIs that will enforce them. Rows are (state, action, next_state, roles).
START_RUN_FROM = [
	"Linked to DHA record",
	"Certificate verification pending",
	"Certificate verified",
	"Scope confirmation",
	"Test plan review",
	"Environment provisioning",
	"Ready to test",
]


def _graph_rows():
	"""Full transition matrix: (state, action, next_state, roles, self_ok)."""
	rows = [
		# Step 1 -> step 2 link chain (applied by link_dha_certificate).
		("Linked to DHA record", "Verify DHA certificate", "Certificate verified", [VENDOR, PLATFORM], 1),
		("Linked to DHA record", "Request certificate verification", "Certificate verification pending", [VENDOR, PLATFORM], 1),
		("Certificate verification pending", "Confirm DHA verification", "Certificate verified", [REVIEWER, PLATFORM], 0),
		("Certificate verification pending", "Return to linked record", "Linked to DHA record", [REVIEWER, PLATFORM], 0),
		("Certificate verified", "Confirm SHA scope", "Scope confirmation", [VENDOR, PLATFORM], 1),
		# Test plan and environment gates (BRD section 4: C -> D -> E).
		("Scope confirmation", "Submit test plan for review", "Test plan review", [VENDOR], 1),
		("Test plan review", "Approve test plan", "Environment provisioning", [REVIEWER, PLATFORM], 0),
		("Environment provisioning", "Confirm environment ready", "Ready to test", [VENDOR, PLATFORM], 1),
		# Run and results (BRD section 4: F -> G).
		("Running", "Publish run results", "Results available", [VENDOR, REVIEWER, PLATFORM], 1),
		("Retest requested", "Start retest run", "Running", [VENDOR, PLATFORM], 1),
		# Reviewer assessment, remediation and retest loop (BRD: H/I/J).
		("Results available", "Begin manual review", "Manual review", [REVIEWER, PLATFORM], 0),
		("Manual review", "Request remediation", "Remediation required", [REVIEWER, PLATFORM], 0),
		("Manual review", "Recommend disposition", "SHA disposition review", [REVIEWER, PLATFORM], 0),
		("Remediation required", "Request retest", "Retest requested", [VENDOR], 1),
		# Disposition and post-validation (BRD: K -> L -> M -> C).
		("SHA disposition review", "Record disposition: passed", "Validation passed", [REVIEWER, PLATFORM], 0),
		("SHA disposition review", "Record disposition: passed with findings", "Validation passed with findings", [REVIEWER, PLATFORM], 0),
		("SHA disposition review", "Record disposition: not validated", "Not validated", [REVIEWER, PLATFORM], 0),
		("SHA disposition review", "Record disposition: blocked", "Blocked", [REVIEWER, PLATFORM], 0),
		("Validation passed", "Authorize surveillance", "Surveillance active", [REVIEWER, PLATFORM], 0),
		("Validation passed with findings", "Authorize surveillance", "Surveillance active", [REVIEWER, PLATFORM], 0),
		("Surveillance active", "Trigger reassessment", "Scope confirmation", [REVIEWER, PLATFORM], 0),
		("Validation passed", "Close validation record", "Closed", [REVIEWER, PLATFORM], 0),
		("Validation passed with findings", "Close validation record", "Closed", [REVIEWER, PLATFORM], 0),
		("Not validated", "Close validation record", "Closed", [REVIEWER, PLATFORM], 0),
		("Blocked", "Close validation record", "Closed", [REVIEWER, PLATFORM], 0),
		("Surveillance active", "Close validation record", "Closed", [REVIEWER, PLATFORM], 0),
	]
	for state in START_RUN_FROM:
		rows.append((state, "Start test run", "Running", [VENDOR, PLATFORM], 1))
	for state in ACKNOWLEDGE_FROM:
		rows.append((state, "Record platform acknowledgement", "Validation passed", [REVIEWER, PLATFORM], 0))
	return rows


def execute():
	actions = sorted({action for _, action, _, _, _ in _graph_rows()})

	for state in STATES:
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc({"doctype": "Workflow State", "workflow_state_name": state}).insert(ignore_permissions=True)

	for action in actions:
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc(
				{"doctype": "Workflow Action Master", "workflow_action_name": action}
			).insert(ignore_permissions=True)

	if frappe.db.exists("Workflow", WORKFLOW_NAME):
		workflow = frappe.get_doc("Workflow", WORKFLOW_NAME)
		workflow.set("states", [])
		workflow.set("transitions", [])
	else:
		workflow = frappe.new_doc("Workflow")

	workflow.update(
		{
			"workflow_name": WORKFLOW_NAME,
			"document_type": DOCUMENT_TYPE,
			"is_active": 1,
			"override_status": 0,
			"send_email_alert": 0,
			"enable_action_confirmation": 0,
			"workflow_state_field": STATE_FIELD,
		}
	)
	state_names = list(STATES)
	# states[0] is the insert baseline for new cases.
	workflow.append("states", {"state": state_names[0], "doc_status": "0", "allow_edit": STATES[state_names[0]]})
	for state in state_names[1:]:
		workflow.append("states", {"state": state, "doc_status": "0", "allow_edit": STATES[state]})

	for state, action, next_state, roles, self_ok in _graph_rows():
		for role in roles:
			workflow.append(
				"transitions",
				{
					"state": state,
					"action": action,
					"next_state": next_state,
					"allowed": role,
					"allow_self_approval": self_ok,
				},
			)

	workflow.save(ignore_permissions=True)
	frappe.clear_cache()
	frappe.db.commit()
