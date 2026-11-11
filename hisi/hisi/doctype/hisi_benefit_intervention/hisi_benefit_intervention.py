# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class HISIBenefitIntervention(Document):
	"""One coded row from the SHA benefits matrices / document-code file.

	``intervention_code`` is the unique lookup key (``autoname =
	field:intervention_code``): ``SHA-19-001``-style intervention codes,
	``PMF-12-001``-style POMSF codes, or the lowercase document/system code
	(``us-abdomen``) when ``benefit_code`` is ``DOC``. Loaded from the
	supplied xlsx matrices by :mod:`hisi.api.ai.benefits_loader`; read at
	report time by :mod:`hisi.api.ai.benefits_registry`, which injects only
	the rows whose codes appear in the claim/preauthorization data.
	"""

	pass
