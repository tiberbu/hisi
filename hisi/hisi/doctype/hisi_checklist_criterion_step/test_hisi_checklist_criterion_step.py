# Copyright (c) 2026, salim and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from hisi.checklist import (
	criteria_of,
	evidence_rule,
	progress_map,
	status_completion,
	step_done,
	step_graph,
	steps_of,
	sub_steps_of,
)

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class IntegrationTestHISIChecklistCriterionStep(IntegrationTestCase):
	"""Steps and sub steps are standalone documents keyed by
	``checklist_version`` + ``criterion_id`` (Frappe only allows one level of
	child tables, so a checklist version holds a link row per predefined
	criterion).

	Fixtures are created inside the class transaction and rolled back with it
	— no test may call anything that commits.
	"""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls.statuses = frappe.get_all(
			"HISI Checklist Status",
			filters={"enabled": 1},
			fields=["name", "status_key"],
			order_by="sort_order asc, status_key asc",
		)
		if not cls.statuses:
			frappe.throw("An enabled checklist status is required for these tests.")
		# Statuses autoname on ``status_key``, so the link name is the status key.
		cls.status = cls.statuses[0].name
		cls.status_key = cls.statuses[0].status_key
		cls.disabled_status = cls._make_disabled_status()

		cls.criterion_one = cls._make_criterion("TC-01", "Criterion one")
		cls.criterion_two = cls._make_criterion("TC-02", "Criterion two")
		cls.version = frappe.get_doc(
			{
				"doctype": "HISI Checklist Version",
				"version": "test-flat-steps",
				"enabled": 1,
				"published": 0,
				"criteria": [
					{"criterion_id": "TC-01"},
					{"criterion_id": "TC-02"},
				],
			}
		).insert(ignore_permissions=True).name

		cls.step_intake = cls._make_step("TC-01", "intake", "Collect evidence", 10)
		cls.step_report = cls._make_step("TC-01", "report", "Write report", 20, requires_note=1)
		cls.step_collect = cls._make_step(
			"TC-02",
			"collect",
			"Upload artefacts",
			10,
			evidence={
				"evidence_required": 1,
				"evidence_type": "File",
				"evidence_hint": "Upload the signed report",
			},
		)
		cls.sub_upload = cls._make_sub_step(cls.step_collect, "upload", "Upload the report", 10)
		cls.sub_confirm = cls._make_sub_step(cls.step_collect, "confirm", "Confirm the upload", 20)

		cls.case = cls._make_case()
		cls.assessment_one = cls._make_assessment("TC-01", "Criterion one")
		cls.assessment_two = cls._make_assessment("TC-02", "Criterion two")

	# ------------------------------------------------------------------ setup

	@classmethod
	def _make_disabled_status(cls) -> str | None:
		if len(cls.statuses) < 2:
			return None
		return frappe.get_doc(
			{
				"doctype": "HISI Checklist Status",
				"status_key": "test_disabled_step_status",
				"label": "Disabled for tests",
				"judgement_type": "Other",
				"sort_order": 99,
				"enabled": 0,
				"is_start": 0,
			}
		).insert(ignore_permissions=True).name

	@classmethod
	def _make_criterion(cls, criterion_id, title) -> str:
		return frappe.get_doc(
			{
				"doctype": "HISI Criterion",
				"criterion_id": criterion_id,
				"title": title,
			}
		).insert(ignore_permissions=True).name

	@classmethod
	def _make_step(cls, criterion_id, step_key, label, sort_order, requires_note=0, evidence=None):
		values = {
			"doctype": "HISI Checklist Criterion Step",
			"checklist_version": cls.version,
			"criterion_id": criterion_id,
			"step_key": step_key,
			"label": label,
			"sort_order": sort_order,
			"checklist_status": cls.status,
			"requires_note": requires_note,
		}
		if evidence:
			values.update(evidence)
		return frappe.get_doc(values).insert(ignore_permissions=True)

	@classmethod
	def _make_sub_step(cls, step, sub_step_key, label, sort_order):
		return frappe.get_doc(
			{
				"doctype": "HISI Checklist Criterion Sub Step",
				"step": step.name,
				"sub_step_key": sub_step_key,
				"label": label,
				"sort_order": sort_order,
			}
		).insert(ignore_permissions=True)

	@classmethod
	def _make_case(cls) -> str:
		vendor = frappe.get_doc(
			{"doctype": "HISI Vendor Organization", "organization_name": "Steps Test Vendor"}
		).insert(ignore_permissions=True).name
		product = frappe.get_doc(
			{
				"doctype": "HISI HMIS Product",
				"vendor": vendor,
				"product_name": "Steps Test Product",
				"release_version": "1.0",
			}
		).insert(ignore_permissions=True).name
		certificate = frappe.get_doc(
			{
				"doctype": "HISI DHA Certification Reference",
				"certificate_id": "STEPS-TEST-CERT",
				"vendor": vendor,
				"product": product,
			}
		).insert(ignore_permissions=True).name
		return frappe.get_doc(
			{
				"doctype": "HISI SHA Validation Case",
				"case_name": "STEPS-TEST-CASE",
				"vendor": vendor,
				"product": product,
				"dha_certificate": certificate,
				"checklist_version": cls.version,
			}
		).insert(ignore_permissions=True).name

	@classmethod
	def _make_assessment(cls, criterion_id, title) -> str:
		return frappe.get_doc(
			{
				"doctype": "HISI Case Criterion Assessment",
				"case": cls.case,
				"criterion_id": criterion_id,
				"criterion_title": title,
			}
		).insert(ignore_permissions=True).name

	def _progress(self, step, assessment, sub_step=None, **values):
		row = {
			"doctype": "HISI Case Criterion Step Progress",
			"case": self.case,
			"assessment": assessment,
			"step": step.name,
			"sub_step": sub_step.name if sub_step else "",
			"completed_by": frappe.session.user,
			"completed_on": frappe.utils.now(),
		}
		row.update(values)
		return frappe.get_doc(row).insert(ignore_permissions=True)

	def _completion(self):
		return status_completion(self.case, self.version)[self.status_key]

	# ------------------------------------------------------------------ tests

	def test_rejects_invalid_step_key(self):
		with self.assertRaises(frappe.ValidationError):
			self._make_step("TC-01", "Not A Slug", "Bad key", 30)

	def test_requires_existing_status(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "HISI Checklist Criterion Step",
					"checklist_version": self.version,
					"criterion_id": "TC-01",
					"step_key": "orphan_status",
					"label": "Orphan status",
					"checklist_status": "no_such_status",
				}
			).insert(ignore_permissions=True)

	def test_rejects_disabled_status(self):
		if not self.disabled_status:
			self.skipTest("Needs a second enabled checklist status to disable one.")
		with self.assertRaisesRegex(frappe.ValidationError, "is disabled"):
			frappe.get_doc(
				{
					"doctype": "HISI Checklist Criterion Step",
					"checklist_version": self.version,
					"criterion_id": "TC-01",
					"step_key": "disabled_status",
					"label": "Disabled status",
					"checklist_status": self.disabled_status,
				}
			).insert(ignore_permissions=True)

	def test_rejects_duplicate_step_key(self):
		with self.assertRaises(frappe.DuplicateEntryError):
			self._make_step("TC-01", "intake", "Duplicate of intake", 40)

	def test_same_step_key_on_another_criterion_is_allowed(self):
		step = self._make_step("TC-02", "intake", "Intake of the second criterion", 30)
		self.assertEqual(step.checklist_version, self.version)
		self.assertEqual(step.criterion_id, "TC-02")

	def test_steps_load_grouped_and_in_order(self):
		by_criterion, sub_steps = step_graph(self.version)
		self.assertEqual([step.step_key for step in by_criterion["TC-01"]], ["intake", "report"])
		self.assertEqual(sub_steps.get(self.step_intake.name, []), [])
		self.assertEqual(
			[sub.sub_step_key for sub in sub_steps[self.step_collect.name]],
			["upload", "confirm"],
		)
		# The lowest sort order always comes first, whatever other tests added.
		self.assertEqual(steps_of(self.version, "TC-02")[0].step_key, "collect")
		self.assertEqual(
			[row.criterion_id for row in criteria_of(self.version)], ["TC-01", "TC-02"]
		)

	def test_evidence_rule_prefers_sub_step(self):
		required, evidence_type, hint = evidence_rule(self.step_collect)
		self.assertEqual((required, evidence_type, hint), (1, "File", "Upload the signed report"))
		# A sub step always wins once it exists, even with nothing configured.
		self.assertEqual(evidence_rule(self.step_collect, self.sub_upload), (0, "Note", ""))
		# A step without sub steps falls back to its own configuration.
		self.assertEqual(evidence_rule(self.step_intake), (0, "Note", ""))

	def test_completion_counts_every_unit(self):
		bucket = self._completion()
		self.assertEqual(bucket["required"], len(steps_of(self.version)))
		self.assertEqual(bucket["completed"], 0)

		# A step without sub steps completes on its own progress row.
		self._progress(self.step_intake, self.assessment_one)
		bucket = self._completion()
		self.assertEqual(bucket["completed"], 1)
		self.assertNotIn("TC-01/intake", bucket["remaining"])
		self.assertFalse(bucket["complete"])

		# A step with sub steps is only done once every sub step is done.
		sub_steps = sub_steps_of([self.step_collect.name])[self.step_collect.name]
		self.assertFalse(step_done(self.step_collect, sub_steps, progress_map(case_name=self.case)))
		self._progress(self.step_collect, self.assessment_two, self.sub_upload)
		self.assertFalse(step_done(self.step_collect, sub_steps, progress_map(case_name=self.case)))
		self._progress(self.step_collect, self.assessment_two, self.sub_confirm)
		self.assertTrue(step_done(self.step_collect, sub_steps, progress_map(case_name=self.case)))

		bucket = self._completion()
		self.assertEqual(bucket["completed"], 2)
		self.assertNotIn("TC-02/collect", bucket["remaining"])

	def test_reopened_unit_is_not_done(self):
		row = self._progress(self.step_report, self.assessment_one)
		self.assertTrue(step_done(self.step_report, [], progress_map(case_name=self.case)))

		row.reopened = 1
		row.reopen_reason = "Wrong artefact"
		row.save(ignore_permissions=True)
		self.assertFalse(step_done(self.step_report, [], progress_map(case_name=self.case)))
		self.assertIn("TC-01/report", self._completion()["remaining"])

		# Re-completing clears the flag and keeps the re-open audit fields.
		row.reopened = 0
		row.save(ignore_permissions=True)
		self.assertTrue(step_done(self.step_report, [], progress_map(case_name=self.case)))
		self.assertNotIn("TC-01/report", self._completion()["remaining"])

	def test_steps_of_criteria_the_checklist_no_longer_lists_are_ignored(self):
		"""A criterion dropped from an edited checklist must stop counting
		even though its steps and its definition still exist."""
		dropped = steps_of(self.version, "TC-02")
		self.assertTrue(dropped)
		before = self._completion()["required"]
		row = frappe.db.get_value(
			"HISI Checklist Criterion",
			{"parent": self.version, "criterion_id": "TC-02"},
			["name", "idx"],
			as_dict=True,
		)
		frappe.db.delete("HISI Checklist Criterion", {"name": row.name})
		try:
			self.assertNotIn("TC-02", [criterion.criterion_id for criterion in criteria_of(self.version)])
			self.assertEqual(self._completion()["required"], before - len(dropped))
		finally:
			frappe.get_doc(
				{
					"doctype": "HISI Checklist Criterion",
					"parenttype": "HISI Checklist Version",
					"parentfield": "criteria",
					"parent": self.version,
					"idx": row.idx,
					"criterion_id": "TC-02",
				}
			).insert(ignore_permissions=True)
		self.assertEqual(self._completion()["required"], before)
		self.assertEqual(
			[criterion.criterion_id for criterion in criteria_of(self.version)],
			["TC-01", "TC-02"],
		)
