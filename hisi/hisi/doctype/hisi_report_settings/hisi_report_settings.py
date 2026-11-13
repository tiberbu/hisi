# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class HISIReportSettings(Document):
	"""Process configuration for the discrepancy report pipeline (the v2
	design's single): AI mode, model routing, budgets, redaction and
	evidence policy, and the BenefitsExchange surface. LLM connectivity
	stays in ``HISI AI Settings``."""

	pass
