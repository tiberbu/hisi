# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class HISILoginCode(Document):
	"""One-time sign-in code issued to an existing account's email.

	The raw code is never persisted - only its SHA-256 hash - so a leaked
	database cannot be replayed into a session."""

	pass
