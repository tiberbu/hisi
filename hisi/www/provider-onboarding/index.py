import frappe

no_cache = True


def get_context(context):
	"""Provider onboarding console - platform operators only.

	A self-contained standalone page (no website chrome): the operator signs
	in with the same MFA flow as the platform (email + password, then the
	emailed one-time code), then manages provider companies and their
	onboarding accounts. Authorization mirrors the platform's platform-role
	gate: System Manager or HISI Reviewer."""
	context.no_cache = 1
	user = frappe.session.user
	if user == "Guest":
		context.logged_user = None
		context.roles = []
		context.platform = False
		context.csrf_token = ""
	else:
		context.logged_user = user
		context.roles = frappe.get_roles(user)
		context.platform = bool(set(context.roles) & {"Administrator", "System Manager", "HISI Reviewer"})
		context.csrf_token = frappe.sessions.get_csrf_token()
	context.title = "Provider onboarding"
