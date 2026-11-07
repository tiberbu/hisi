"""Seed the field surveillance templates (one per category).

The wizard copies the category template's steps into each new case, and the
Surveillance templates screen edits these documents from then on - so this
patch only *creates* the starting templates and never touches existing ones
(HQ edits must survive re-runs).

Starting steps per the Field Surveillance design language (section 7);
target days are the default timeline and can be re-tuned in the templates
screen. Note templates: three per category, each a clear instruction, what
evidence to attach and when to call HQ.
"""

import frappe

STARTING_STEPS = {
	"Claim": [
		("Confirm the claim exists and matches the submission", 1),
		("Confirm patient identity and eligibility on the date of service", 1),
		("Confirm the patient was treated at the facility", 1),
		("Compare billed services with clinical records", 2),
		("Verify the practitioner", 1),
		("Interview where needed", 1),
		("Record findings with evidence", 1),
	],
	"Patient visit": [
		("Confirm each patient’s identity", 1),
		("Confirm each visit occurred", 1),
		("Check diagnosis, treatment and medicines against records", 2),
		("Confirm records sent to SHR match", 1),
		("Interview patients including any payment demanded", 1),
		("Record findings", 1),
	],
	"Overall facility": [
		("Review claim volumes and patterns", 2),
		("Compare with capacity, staffing and beds", 1),
		("Inspect registers, pharmacy and laboratory records", 2),
		("Verify practitioners and rosters", 1),
		("Check completeness of SHR submissions", 1),
		("Interview management", 1),
		("Record findings and recommendations", 1),
	],
}

NOTE_TEMPLATES = {
	"Claim": [
		(
			"Standard claim review",
			"Please work through the checklist in order. Attach every document you rely on "
			"(claim form, clinical records, identity verification) as evidence. If the facility or "
			"the practitioner cannot be reached within 48 hours, call HQ before the due date.",
		),
		(
			"Urgent claim review",
			"This case is urgent: start immediately and send HQ a short update within 24 hours. "
			"Attach all documents you rely on as evidence. If the claim amount or the patient’s "
			"identity is in doubt, call HQ before submitting findings.",
		),
		(
			"Follow-up claim review",
			"This is a follow-up on a previously reviewed claim. Re-check only the items listed in "
			"the steps, attach fresh evidence where records have changed, and note any differences "
			"from the last review in your summary.",
		),
	],
	"Patient visit": [
		(
			"Standard visit review",
			"Please confirm each patient and visit in the scope list, then check the clinical "
			"details. Attach copies of the records you compare against as evidence. If a patient "
			"refuses to be interviewed, record that and continue with the remaining steps.",
		),
		(
			"Urgent visit review",
			"This case is urgent: start immediately and send HQ a short update within 24 hours. "
			"Attach the records you compare against as evidence. If any patient reports a payment "
			"demanded for treatment, call HQ the same day.",
		),
		(
			"Follow-up visit review",
			"This is a follow-up on a previously reviewed visit. Re-check only the items listed in "
			"the steps, attach fresh evidence where records have changed, and note any differences "
			"from the last review in your summary.",
		),
	],
	"Overall facility": [
		(
			"Standard facility review",
			"Please work through the checklist in order, starting with the claim volumes. Attach "
			"photographs of registers, rosters and any documents you rely on as evidence. If "
			"management is not available on site, call HQ before the due date.",
		),
		(
			"Urgent facility review",
			"This case is urgent: start immediately and send HQ a short update within 24 hours. "
			"Attach photographs and copies of the records you inspect as evidence. If you find "
			"signs of systematic falsification, call HQ the same day.",
		),
		(
			"Follow-up facility review",
			"This is a follow-up on a previously reviewed facility. Re-check only the items listed "
			"in the steps, attach fresh evidence where records have changed, and note any "
			"differences from the last review in your summary.",
		),
	],
}


def execute():
	for category, steps in STARTING_STEPS.items():
		if frappe.db.exists("HISI Surveillance Template", category):
			continue
		doc = frappe.get_doc({"doctype": "HISI Surveillance Template", "category": category})
		for index, (text, target_days) in enumerate(steps):
			doc.append("template_steps", {"text": text, "target_days": target_days, "sort": index})
		for name, text in NOTE_TEMPLATES[category]:
			doc.append("note_templates", {"template_name": name, "text": text})
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
