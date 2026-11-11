# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class HISIAISettings(Document):
	"""Single-doctype configuration for the AI endpoints (self-contained).

	Holds the LLM host settings (base URL, API key, model, timeouts) and the
	discrepancy-report knobs (system prompt, data sources, record cap). Every
	runtime key has a documented default in ``hisi.api.ai.settings`` — an
	unset field means "use the default", and an unset ``llm_base_url`` means
	the AI endpoints fail closed (``LLM_NOT_CONFIGURED``).
	"""

	pass
