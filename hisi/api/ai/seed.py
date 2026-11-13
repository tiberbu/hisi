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
		"TIMING_ANOMALY",
		"Timing anomaly",
		"low",
		"Dates that are out of logical order: a claim submitted before its service date, or a preauthorization decided after the claim it was supposed to precede.",
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
	(
		"CLAIM_NOT_DISPATCHED",
		"Claim not sent to payer",
		"medium",
		"The claim's status (per the HISI Claim Status config) means it was not successfully sent to the "
		"payer system (e.g. failed_dispatch or queued_for_validation). Surfaced as context so payment-oriented "
		"findings on the claim are read as provisional, not as a payment discrepancy.",
		"claim_not_dispatched",
	),
	# Phase C — claims-sent vs SHR-data-available (the visit's claims
	# cross-checked against what the shared health record actually holds for
	# that visit, in both directions).
	(
		"CLAIM_WITHOUT_SHR_DATA",
		"Claim sent with no clinical record for the visit",
		"high",
		"A claim bills service for a visit, but the shared health record holds no clinical resource for "
		"that visit at all — the claim is not supported by any recorded data for the visit.",
		"claim_without_shr_data",
	),
	(
		"SHR_DATA_WITHOUT_CLAIM",
		"Clinical record for a visit with no claim",
		"low",
		"The shared health record holds clinical resources for a consented visit, but no claim or "
		"preauthorization references that visit — recorded service that was never billed to the payer.",
		"shr_data_without_claim",
	),
	# Phase C+ — cross-facility duplicate billing (same codes + overlapping
	# dates, different facilities; the cross-billing fraud pattern).
	(
		"CROSS_FACILITY_DUPLICATE",
		"Duplicate billing across facilities",
		"high",
		"Two claims bill the same intervention(s) with overlapping service dates against two different "
		"facilities. A classic cross-billing pattern — verify whether it is a legitimate multi-provider "
		"referral before escalation.",
		"cross_facility_duplicate",
	),
	# Consent lifecycle integrity: the consent was granted after the service.
	(
		"CONSENT_AFTER_SERVICE",
		"Consent created after the service",
		"medium",
		"The claim's service date predates the creation of the visit's consent record beyond tolerance — "
		"consent granted after the fact warrants review.",
		"consent_after_service",
	),
	# Preauthorization window: the service fell outside the authorized period.
	(
		"PREAUTH_PERIOD_MISMATCH",
		"Service outside the preauthorization window",
		"medium",
		"The claim's service dates do not overlap the related preauthorization's authorized service period "
		"beyond tolerance — the service was rendered outside the authorized window.",
		"preauth_period_mismatch",
	),
	# Medication triangulation: claimed medication with no prescription-side documentation.
	(
		"MEDICATION_NOT_DOCUMENTED",
		"Claimed medication with no prescription documentation",
		"medium",
		"The claim bills medication-family interventions, the visit's clinical record is documented, but it "
		"holds no MedicationRequest resource — no prescription-side documentation for the claimed medication.",
		"medication_not_documented",
	),
	# Reserved — blocked on DHA payload fields (the claim side carries no
	# expiry / scheme data today). Seeded rule_type "off": the catalog is
	# complete, the rule activates by config the moment the fields exist.
	(
		"PREAUTH_EXPIRED",
		"Service after preauthorization expiry",
		"medium",
		"The claim's service date falls after the related preauthorization's validity expiry date. (Reserved: "
		"activates when the preauth payload exposes a validity/expiry field.)",
		"preauth_expired",
	),
	(
		"SCHEME_MISMATCH",
		"Claim on an inactive scheme",
		"high",
		"The claim bills a scheme that is not active for the member on the eligibility record. (Reserved: "
		"activates when the claim payload carries a scheme field.)",
		"scheme_mismatch",
	),
)


#: Tag codes seeded switched off: the deterministic check is blocked on DHA
#: payload fields (no preauth expiry / claim scheme data today). They activate
#: by config (rule_type code) the moment the fields exist.
RESERVED_TAG_CODES = frozenset({"PREAUTH_EXPIRED", "SCHEME_MISMATCH"})


def ensure_default_tags() -> int:
	"""Create the missing default tags; returns the number created."""
	existing = {row.tag_code for row in frappe.get_all(TAG, fields=["tag_code"])}
	created = 0
	for tag_code, label, severity, instruction, rule_key in DEFAULT_TAGS:
		if tag_code in existing:
			continue
		# Reserved tags (blocked on DHA payload fields) are seeded switched off
		# — the catalog is complete, the rule activates by config later.
		rule_type = "off" if tag_code in RESERVED_TAG_CODES else "code"
		doc = frappe.get_doc(
			{
				"doctype": TAG,
				"tag_code": tag_code,
				"label": label,
				"severity": severity,
				"instruction": instruction,
				"enabled": 1,
				"rule_type": rule_type,
				"rule_key": rule_key,
				"severity_policy": "fixed",
				"version": 1,
			}
		)
		doc.insert(ignore_permissions=True)
		created += 1
	# Legacy contribution-gap tags stop driving findings: contributions are
	# context only (the payer makes contribution decisions). A pre-existing
	# tag is switched off (kept for history) instead of deleted.
	for row in frappe.get_all(TAG, fields=["name", "tag_code", "rule_type"]):
		if str(row.tag_code or "").strip().upper() == "CONTRIBUTION_GAP" and str(row.rule_type or "code") == "code":
			frappe.db.set_value(TAG, row.name, {"rule_type": "off", "version": 2})
	if created or not frappe.flags.in_test:
		frappe.db.commit()
	if created and not frappe.flags.in_test:
		frappe.logger("hisi.ai").info(f"seeded {created} default discrepancy tag(s)")
	return created


CLAIM_STATUS_CONFIG = "HISI Claim Status"

#: Known DHA claim statuses and whether they mean the claim was successfully
#: sent to the payer. ``sent_to_payer`` False rows are the ones that must be
#: surfaced as context (the claim is not yet in play, so payment-oriented
#: findings must not be read as payment discrepancies).
DEFAULT_CLAIM_STATUSES = (
	(
		"failed_dispatch",
		"Failed Dispatch",
		0,
		"Not successfully sent to the payer system (dispatch failed). The claim is not yet "
		"in play; amount and preauthorization findings for this claim are provisional and "
		"should not be read as a payment discrepancy until dispatch succeeds.",
	),
	(
		"queued_for_validation",
		"Queued for Validation",
		0,
		"Queued for validation and not yet sent to the payer. The claim is not yet in play; "
		"amount and preauthorization findings for this claim are provisional.",
	),
	(
		"submitted",
		"Submitted",
		1,
		"",
	),
	(
		"in_review",
		"In Review",
		1,
		"",
	),
	(
		"approved",
		"Approved",
		1,
		"",
	),
	(
		"rejected",
		"Rejected",
		1,
		"Rejected by the payer / validator.",
	),
	(
		"paid",
		"Paid",
		1,
		"",
	),
)


def ensure_claim_status_config() -> int:
	"""Create the missing default claim-status rows; returns the count created.

	Idempotent — existing rows (by ``claim_status``) are left untouched, so an
	operator can override the meaning/note of any status after seeding."""
	existing = {row.claim_status for row in frappe.get_all(CLAIM_STATUS_CONFIG, fields=["claim_status"])}
	created = 0
	for status, label, sent, note in DEFAULT_CLAIM_STATUSES:
		if status in existing:
			continue
		frappe.get_doc(
			{
				"doctype": CLAIM_STATUS_CONFIG,
				"claim_status": status,
				"label": label,
				"sent_to_payer": sent,
				"note": note,
			}
		).insert(ignore_permissions=True)
		created += 1
	if created and not frappe.flags.in_test:
		frappe.db.commit()
		frappe.logger("hisi.ai").info(f"seeded {created} claim-status config row(s)")
	return created
