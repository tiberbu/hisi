import re

import frappe
from frappe.model.document import Document

from hisi.checklist import validate_evidence_config

SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


class HISIChecklistCriterionStep(Document):
	def validate(self):
		self.step_key = (self.step_key or "").strip().lower()
		self.criterion_id = (self.criterion_id or "").strip()
		if not re.match(SLUG_PATTERN, self.step_key or ""):
			frappe.throw(
				"Step Key must be a lowercase slug, e.g. test_plan.",
				frappe.ValidationError,
			)
		self.sort_order = max(0, int(self.sort_order or 0))
		self._validate_unique_key()
		self._validate_status()
		validate_evidence_config(self)

	def _validate_unique_key(self):
		if not (self.checklist_version and self.criterion_id and self.step_key):
			return
		filters = {
			"checklist_version": self.checklist_version,
			"criterion_id": self.criterion_id,
			"step_key": self.step_key,
		}
		# A new document may already be named after the row it collides with,
		# so only an existing row is allowed to exclude itself.
		if not self.is_new():
			filters["name"] = ["!=", self.name or ""]
		duplicate = frappe.db.exists("HISI Checklist Criterion Step", filters)
		if duplicate:
			frappe.throw(
				f"Step '{self.step_key}' already exists on criterion '{self.criterion_id}' "
				f"of checklist {self.checklist_version}.",
				frappe.DuplicateEntryError,
			)

	def _validate_status(self):
		if not self.checklist_status:
			frappe.throw("Checklist Status is required.", frappe.ValidationError)
		status = frappe.db.get_value(
			"HISI Checklist Status",
			self.checklist_status,
			["status_key", "enabled"],
			as_dict=True,
		)
		if not status:
			frappe.throw(
				f"Checklist Status '{self.checklist_status}' does not exist.",
				frappe.ValidationError,
			)
		if not status.enabled:
			frappe.throw(
				f"Checklist Status '{self.checklist_status}' is disabled.",
				frappe.ValidationError,
			)
