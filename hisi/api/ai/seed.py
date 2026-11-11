# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""Idempotent seeding of the default discrepancy tag catalog.

``ensure_default_tags`` creates the built-in checks (tag code, label,
severity, instruction) when missing — one insert per absent tag, so a
second call is a no-op. Existing tags are never overwritten: an operator's
edited instruction or severity survives. The API endpoints call it before
reading the catalog so a fresh site has a usable vocabulary on first use.
"""

import frappe

TAG = "HISI Discrepancy Tag"

#: (tag_code, label, severity, instruction) — the built-in catalog.
DEFAULT_TAGS = (
	(
		"ORPHAN_CLAIM",
		"Orphan claim",
		"high",
		"A claim with no matching visit consent record, or no supporting visit evidence at all — a claim that cannot be tied to a consented encounter.",
	),
	(
		"UNCOVERED_SERVICE",
		"Uncovered service",
		"medium",
		"A claimed service dated outside the active coverage window on the eligibility record, or for a scheme that is not active for the member.",
	),
	(
		"AMOUNT_MISMATCH",
		"Amount mismatch",
		"medium",
		"A claimed amount that deviates materially from the priced amount on the linked claim or preauthorization record.",
	),
	(
		"MISSING_PREAUTH",
		"Missing preauthorization",
		"high",
		"An expensive procedure or service claimed without a prior authorization record, where one is expected for that service.",
	),
	(
		"DUPLICATE_CLAIM",
		"Duplicate claim",
		"high",
		"Two or more claims for the same service with the same dates, facility, and procedure, indicating double billing.",
	),
	(
		"CONTRIBUTION_GAP",
		"Contribution gap",
		"medium",
		"A gap in the member's contribution ledger: consecutive periods with no payment recorded where contributions are expected.",
	),
	(
		"TIMING_ANOMALY",
		"Timing anomaly",
		"low",
		"Dates that are out of logical order: a claim submitted before its service date, a preauthorization decided after the claim, or a contribution posted for a future period.",
	),
	(
		"ELIGIBILITY_MISMATCH",
		"Eligibility mismatch",
		"high",
		"A claim or preauthorization submitted while the member is inactive or terminated, or with no active scheme matching the claim on the eligibility record.",
	),
)


def ensure_default_tags() -> int:
	"""Create the missing default tags; returns the number created."""
	existing = {row.tag_code for row in frappe.get_all(TAG, fields=["tag_code"])}
	created = 0
	for tag_code, label, severity, instruction in DEFAULT_TAGS:
		if tag_code in existing:
			continue
		doc = frappe.get_doc(
			{
				"doctype": TAG,
				"tag_code": tag_code,
				"label": label,
				"severity": severity,
				"instruction": instruction,
				"enabled": 1,
			}
		)
		doc.insert(ignore_permissions=True)
		created += 1
	if created and not frappe.flags.in_test:
		frappe.db.commit()
		frappe.logger("hisi.ai").info(f"seeded {created} default discrepancy tag(s)")
	return created
