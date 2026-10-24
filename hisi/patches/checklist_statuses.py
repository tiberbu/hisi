"""Seed the default payer validation statuses and their workflow graph.

HISI Checklist Status rows are configuration, not code: the administrator
Checklist page and the review APIs read them. This patch installs a sensible
default graph once (idempotent — existing keys are left untouched so site
edits survive re-migrate):

    in_review ──> approved
              ──> rejected
              ──> more_info_required ──> in_review

``in_review`` starts the workflow; ``approved``/``rejected`` are terminal.
``more_info_required`` demands a reviewer comment.
"""

import frappe

# key, label, judgement_type, sort_order, is_start, requires_comment, description
STATUSES = [
	(
		"in_review",
		"In review",
		"Other",
		10,
		1,
		0,
		"Vendor is under payer validation review. Starting status for new organizations.",
	),
	(
		"more_info_required",
		"More information required",
		"Request More Info",
		20,
		0,
		1,
		"Sent back to the vendor: reviewer must describe what is missing.",
	),
	("approved", "Approved", "Approve", 30, 0, 0, "Validation complete and accepted."),
	("rejected", "Rejected", "Reject", 40, 0, 0, "Validation complete and not accepted."),
]

# key -> [(to_status_key, action_label)]
TRANSITIONS = {
	"in_review": [
		("approved", "Approve"),
		("rejected", "Reject"),
		("more_info_required", "Request more info"),
	],
	"more_info_required": [("in_review", "Resume review")],
}


def execute():
	if not frappe.db.exists("DocType", "HISI Checklist Status"):
		return

	for key, label, judgement, sort_order, is_start, requires_comment, description in STATUSES:
		if frappe.db.exists("HISI Checklist Status", key):
			continue
		frappe.get_doc(
			{
				"doctype": "HISI Checklist Status",
				"status_key": key,
				"label": label,
				"judgement_type": judgement,
				"sort_order": sort_order,
				"enabled": 1,
				"is_start": is_start,
				"requires_comment": requires_comment,
				"description": description,
			}
		).insert(ignore_permissions=True)

	# Attach transitions on a second pass so Link targets all exist.
	for key, edges in TRANSITIONS.items():
		if not frappe.db.exists("HISI Checklist Status", key):
			continue
		doc = frappe.get_doc("HISI Checklist Status", key)
		existing = {(row.to_status, row.action_label or "") for row in doc.get("allowed_next") or []}
		changed = False
		for to_status, action_label in edges:
			if not frappe.db.exists("HISI Checklist Status", to_status):
				continue
			if (to_status, action_label) in existing:
				continue
			# Skip duplicate edges regardless of label wording.
			if any(row.to_status == to_status for row in doc.get("allowed_next") or []):
				continue
			doc.append("allowed_next", {"to_status": to_status, "action_label": action_label})
			changed = True
		if changed:
			doc.save(ignore_permissions=True)

	frappe.db.commit()
