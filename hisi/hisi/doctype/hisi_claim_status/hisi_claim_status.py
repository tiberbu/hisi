# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class HISIClaimStatus(Document):
	"""Configuration row mapping one raw DHA ``claim_status`` value to its
	payment-relevant meaning.

	``sent_to_payer`` is the key signal: a status like ``failed_dispatch`` or
	``queued_for_validation`` means the claim was NOT successfully sent to the
	payer system, so a report analyzing that claim must not present amount or
	preauthorization findings as payment discrepancies — the claim is not yet
	in play. The optional ``note`` is injected into the report as context so a
	reader is never misled about the claim's state.
	"""

	pass
