from frappe.model.document import Document


class HISIOfficerInvite(Document):
	"""One-time sign-in code for an officer invitation.

	Created when a dispatcher invites an officer - either a brand-new HISI
	account or an existing user being linked to another company. The code is
	single-use: confirming it activates the membership (new accounts exchange
	it for a first-password reset key; existing accounts receive a one-time
	sign-in grant). The code is never set as a user's password and has no
	password-reset effect of its own.
	"""

	pass
