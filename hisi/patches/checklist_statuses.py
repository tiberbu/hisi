"""Seed the default payer validation statuses and their workflow graph.

HISI Checklist Status rows are configuration, not code: the administrator
Checklist page and the review APIs read them. This patch installs a sensible
default graph once (idempotent — existing keys and edges are left untouched
so site edits survive re-migrate):

    pending ──> in_progress ──> in_review ──> approved
                                         ──> rejected
                                         ──> more_info_required ──> in_progress

``pending`` is the default: the state a checklist sits in before the vendor
starts the verification journey, and the status new steps are tagged with
unless the payload says otherwise. ``in_progress`` is where the vendor works
through them; a status can only be entered once every step of that status is
finished (see ``hisi.checklist.status_completion``). ``approved``/``rejected``
are terminal and ``more_info_required`` demands a reviewer comment. Edges
added by earlier releases (``pending ──> in_review``,
``more_info_required ──> in_review``) are never removed here.
"""

import frappe

# key, label, judgement_type, sort_order, is_start, requires_comment, description
STATUSES = [
	(
		"pending",
		"Pending",
		"Other",
		0,
		1,
		0,
		"Vendor is waiting to start payer validation. Has already onboarded to the process",
	),
	(
		"in_progress",
		"In progress",
		"Other",
		5,
		0,
		0,
		"Vendor is working through the checklist steps for this criterion.",
	),
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
	"pending": [("in_progress", "Start work")],
	"in_progress": [("in_review", "Submit for review")],
	"in_review": [
		("approved", "Approve"),
		("rejected", "Reject"),
		("more_info_required", "Request more info"),
	],
	"more_info_required": [
		("in_review", "Resume review"),
		("in_progress", "Address feedback"),
	],
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
