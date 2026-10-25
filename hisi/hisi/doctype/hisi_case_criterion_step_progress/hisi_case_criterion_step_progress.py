import frappe
from frappe.model.document import Document

# Link field -> the checklist doctype it points at (for snapshot values).
LINK_DOCTYPES = {
	"step": "HISI Checklist Criterion Step",
	"sub_step": "HISI Checklist Criterion Sub Step",
}


class HISICaseCriterionStepProgress(Document):
	"""Completion of one step (or sub step) of one criterion on one case.

	One row per ``(assessment, step, sub step)``; a re-open flips ``reopened``
	on the row instead of deleting it, and the next completion clears the flag
	again while keeping the re-open audit fields.
	"""

	def validate(self):
		self.step_key = (self.step_key or "").strip() or self._snapshot("step", "step_key")
		self.sub_step_key = (self.sub_step_key or "").strip() or (
			self._snapshot("sub_step", "sub_step_key") if self.sub_step else ""
		)
		self.step_label = self.step_label or self._snapshot("step", "label")
		self._validate_unique_progress()

	def _snapshot(self, link_field: str, fieldname: str) -> str:
		target = getattr(self, link_field, None)
		if not target:
			return ""
		return frappe.db.get_value(LINK_DOCTYPES[link_field], target, fieldname) or ""

	def _validate_unique_progress(self):
		"""Uniqueness ignores the empty-vs-NULL spelling of ``sub_step``."""
		if not (self.assessment and self.step):
			return
		sub_step = self.sub_step or ""
		for row in frappe.get_all(
			"HISI Case Criterion Step Progress",
			filters={"assessment": self.assessment, "step": self.step},
			fields=["name", "sub_step"],
		):
			if (row.sub_step or "") == sub_step and row.name != (self.name or ""):
				frappe.throw(
					f"Progress for step '{self.step_key}' is already recorded on this criterion.",
					frappe.DuplicateEntryError,
				)
