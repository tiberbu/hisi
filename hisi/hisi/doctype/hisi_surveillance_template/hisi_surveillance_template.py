from frappe.model.document import Document


class HISISurveillanceTemplate(Document):
	"""HQ-managed starting template for one surveillance category: the step
	timeline (steps with target days) and the note templates offered at
	assignment. Edits apply to new requests only - existing cases keep the
	steps and notes they were created with."""

	def validate(self):
		if self.template_steps:
			self.total_days = sum(int(step.target_days or 0) for step in self.template_steps)
		else:
			self.total_days = 0
