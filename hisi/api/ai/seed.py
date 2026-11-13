# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""Idempotent seeding of the default discrepancy tag catalog.

``ensure_default_tags`` creates the built-in checks (tag code, label,
severity, instruction, rule key) when missing — one insert per absent tag,
so a second call is a no-op. Existing tags are never overwritten: an
operator's edited instruction or severity survives. The API endpoints call
it before reading the catalog so a fresh site has a usable vocabulary on
first use.

The v2 catalog adds the deterministic rule metadata (``rule_type``,
``rule_key``, ``version``) to every built-in and the Phase B clinical
triangulation checks (``TRI_*``).
"""

import frappe

TAG = "HISI Discrepancy Tag"

#: (tag_code, label, severity, instruction, rule_key) — the built-in
#: catalog. ``rule_key`` maps the tag to its deterministic evaluator
#: (lower-cased tag code); the triangulation checks are the Phase B
#: clinical<->claim cross-checks.
DEFAULT_TAGS = (
	(
		"ORPHAN_CLAIM",
		"Orphan claim",
		"high",
		"A claim with no matching visit consent record, or no supporting visit evidence at all — a claim that cannot be tied to a consented encounter.",
		"orphan_claim",
	),
	(
		"UNCOVERED_SERVICE",
		"Uncovered service",
		"medium",
		"A claimed service dated outside the active coverage window on the eligibility record, or for a scheme that is not active for the member.",
		"uncovered_service",
	),
	(
		"AMOUNT_MISMATCH",
		"Amount mismatch",
		"medium",
		"A claimed amount that deviates materially from the priced amount on the linked claim or preauthorization record.",
		"amount_mismatch",
	),
	(
		"MISSING_PREAUTH",
		"Missing preauthorization",
		"high",
		"An expensive procedure or service claimed without a prior authorization record, where one is expected for that service.",
		"missing_preauth",
	),
	(
		"DUPLICATE_CLAIM",
		"Duplicate claim",
		"high",
		"Two or more claims for the same service with the same dates, facility, and procedure, indicating double billing.",
		"duplicate_claim",
	),
	(
		"CONTRIBUTION_GAP",
		"Contribution gap",
		"medium",
		"A gap in the member's contribution ledger: consecutive periods with no payment recorded where contributions are expected.",
		"contribution_gap",
	),
	(
		"TIMING_ANOMALY",
		"Timing anomaly",
		"low",
		"Dates that are out of logical order: a claim submitted before its service date, a preauthorization decided after the claim, or a contribution posted for a future period.",
		"timing_anomaly",
	),
	(
		"ELIGIBILITY_MISMATCH",
		"Eligibility mismatch",
		"high",
		"A claim or preauthorization submitted while the member is inactive or terminated, or with no active scheme matching the claim on the eligibility record.",
		"eligibility_mismatch",
	),
	# Phase B — clinical<->claim triangulation (the claim's clinical
	# assertions cross-checked against the visit's clinical record).
	(
		"TRI_DIAGNOSIS",
		"Diagnosis not in clinical record",
		"medium",
		"The claim's diagnosis codes are not documented by any Condition resource in the claim's visit clinical record.",
		"tri_diagnosis",
	),
	(
		"TRI_INTERVENTION",
		"Surgery not in clinical record",
		"medium",
		"A claimed surgical intervention has no Procedure or ServiceRequest resource in the claim's visit clinical record.",
		"tri_intervention",
	),
	(
		"TRI_DATES",
		"Service dates outside the recorded encounter",
		"medium",
		"The claim's service dates fall outside the Encounter period recorded in the claim's visit clinical record.",
		"tri_dates",
	),
	(
		"TRI_FACILITY",
		"Facility differs from consented facility",
		"medium",
		"The claim's facility differs from the facility named on the consent record for the same visit.",
		"tri_facility",
	),
)


def ensure_default_tags() -> int:
	"""Create the missing default tags; returns the number created."""
	existing = {row.tag_code for row in frappe.get_all(TAG, fields=["tag_code"])}
	created = 0
	for tag_code, label, severity, instruction, rule_key in DEFAULT_TAGS:
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
				"rule_type": "code",
				"rule_key": rule_key,
				"severity_policy": "fixed",
				"version": 1,
			}
		)
		doc.insert(ignore_permissions=True)
		created += 1
	if created and not frappe.flags.in_test:
		frappe.db.commit()
		frappe.logger("hisi.ai").info(f"seeded {created} default discrepancy tag(s)")
	return created
