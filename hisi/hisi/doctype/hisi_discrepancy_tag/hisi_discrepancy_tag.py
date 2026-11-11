# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class HISIDiscrepancyTag(Document):
	"""One discrepancy check in the AI report vocabulary.

	``tag_code`` is the stable identifier the LLM cites in its findings
	(``autoname = field:tag_code``); ``instruction`` is the check wording
	sent to the model; ``severity`` is the default severity for findings
	rather than that tag."""

	pass
