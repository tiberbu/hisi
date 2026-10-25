import re

import frappe
from frappe.model.document import Document

from hisi.checklist import validate_evidence_config

SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


class HISIChecklistCriterionSubStep(Document):
	def validate(self):
		self.sub_step_key = (self.sub_step_key or "").strip().lower()
		if not re.match(SLUG_PATTERN, self.sub_step_key or ""):
			frappe.throw(
				"Sub Step Key must be a lowercase slug, e.g. upload_report.",
				frappe.ValidationError,
			)
		self.sort_order = max(0, int(self.sort_order or 0))
		self._validate_unique_key()
		validate_evidence_config(self)

	def _validate_unique_key(self):
		if not (self.step and self.sub_step_key):
			return
		filters = {"step": self.step, "sub_step_key": self.sub_step_key}
		# A new document may already be named after the row it collides with,
		# so only an existing row is allowed to exclude itself.
		if not self.is_new():
			filters["name"] = ["!=", self.name or ""]
		duplicate = frappe.db.exists("HISI Checklist Criterion Sub Step", filters)
		if duplicate:
			frappe.throw(
				f"Sub step '{self.sub_step_key}' already exists on step '{self.step}'.",
				frappe.DuplicateEntryError,
			)
