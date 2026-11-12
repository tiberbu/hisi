"""Single source of brand copy for server-rendered surfaces.

The web app keeps its own equivalent (``hisi_app/lib/brand.ts`` drives the
UI copy, logos and meta from one file). This module is the backend twin for
the surfaces rendered on the server — transactional email and the PDF
report — so a brand change touches exactly two files instead of every
template. Brand wording must not be hardcoded anywhere else; templates
refer to it through the ``{{ brand }}`` / ``{{ brand_company }}`` variables
injected by :func:`hisi.utils.render_email_template`.
"""

#: Product name shown to users (the lockup logo carries the visual mark).
BRAND_NAME = "Clinical Validation Portal"

#: Company name (footer line, sign-off).
COMPANY_NAME = "Tiberbu"

#: Brand for the discrepancy report deliverable (the PDF masthead and
#: footer). In-app and email surfaces keep their own wording.
REPORT_BRAND = "Clinical Validation Portal"


def context() -> dict:
	"""Jinja variables for brand-bearing templates.

	Always merged into the email-template render context, so any template can
	use ``{{ brand }}`` / ``{{ brand_company }}`` without the caller passing
	them. (Not ``company`` — the officer-invite template already uses that
	name for the invitee's organization, and caller args would win the
	collision.)"""
	return {"brand": BRAND_NAME, "brand_company": COMPANY_NAME}
