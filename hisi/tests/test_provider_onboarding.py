from unittest.mock import patch

import frappe
from frappe.auth import CookieManager, LoginManager
from frappe.utils.password import check_password, update_password

from hisi.api import provider_onboarding
from hisi.api.surveillance import onboarding
from hisi.tests.test_onboarding import _RequestStub
from hisi.tests.test_surveillance import SurveillanceTestCase


class TestProviderOnboarding(SurveillanceTestCase):
	"""Provider onboarding console: platform operators create provider
	companies and their onboarding (admin) accounts. The admin confirms the
	emailed one-time code and gains the HISI Insurance Provider role plus the
	active Employee link; from there they invite their own members through
	the regular Team surface."""

	def setUp(self):
		super().setUp()
		# The invite flow signs users in through frappe.local.login_manager,
		# which only exists on real HTTP requests; emulate the request context
		# so login_as() works under the test runner (same as the officer suite).
		if not hasattr(frappe.local, "request"):
			frappe.local.request = _RequestStub()
		frappe.local.cookie_manager = CookieManager()
		frappe.local.login_manager = LoginManager()
		self.mail_patcher = patch("frappe.sendmail")
		self.sent_mail = self.mail_patcher.start()
		self.operator = self.make_user("platform.op@acme.example", ["System Manager"])

	def tearDown(self):
		self.mail_patcher.stop()
		super().tearDown()

	def mail_kwargs(self):
		calls = self.sent_mail.call_args
		return calls.kwargs if calls else {}

	def pending_invite(self, email, company=None):
		filters = {"email": email, "status": "Pending"}
		if company:
			filters["company"] = company
		return frappe.get_all(
			"HISI Officer Invite",
			filters=filters,
			fields=["name", "code", "company", "role", "account_created"],
			order_by="creation desc",
			limit=1,
		)

	# -- authorization ------------------------------------------------------

	def test_list_requires_platform_role(self):
		# @api_endpoint converts the raised APIResponse (a ValidationError
		# subclass) into frappe.local.response, so assert on the response.
		frappe.set_user("Guest")
		provider_onboarding.list_provider_companies()
		self.assertEqual(frappe.local.response.get("http_status_code"), 401)
		frappe.set_user(self.dispatcher)
		provider_onboarding.list_provider_companies()
		self.assertEqual(frappe.local.response.get("http_status_code"), 403)

	def test_list_returns_company_rows_with_onboarding_state(self):
		frappe.set_user(self.operator)
		provider_onboarding.list_provider_companies()
		self.assertEqual(frappe.local.response.get("status"), "success")
		data = frappe.local.response["data"]
		by_company = {row["company"]: row for row in data["companies"]}
		self.assertIn(self.org, by_company)
		row = by_company[self.org]
		self.assertEqual(row["active_members"], 1)  # the dispatcher
		self.assertFalse(row["admin_ready"])
		self.assertEqual(row["pending_invites"], 0)
		self.assertEqual(row["pending_admins"], [])
		# Audit event recorded.
		events = frappe.get_all("HISI Surveillance Event", filters={"event_type": "provider_companies_listed"}, fields=["name"])
		self.assertGreaterEqual(len(events), 1)

	def test_create_and_onboard_require_platform_role(self):
		frappe.set_user(self.dispatcher)
		provider_onboarding.create_provider_company(company_name="Gamma Assurance")
		self.assertEqual(frappe.local.response.get("http_status_code"), 403)
		provider_onboarding.onboard_provider_admin(company=self.org, email="x@acme.example")
		self.assertEqual(frappe.local.response.get("http_status_code"), 403)

	# -- company creation ----------------------------------------------------

	def test_create_provider_company(self):
		frappe.set_user(self.operator)
		before = self.event_count()
		provider_onboarding.create_provider_company(company_name="Acacia Assurance")
		self.assertEqual(frappe.local.response.get("status"), "success")
		self.assertEqual(frappe.local.response["data"]["company"], "Acacia Assurance")
		values = frappe.db.get_value("Company", "Acacia Assurance", ["abbr", "country", "default_currency", "valuation_method"], as_dict=True)
		self.assertTrue(values["abbr"])
		self.assertEqual(values["country"], "Kenya")
		self.assertEqual(values["default_currency"], "KES")
		self.assertEqual(values["valuation_method"], "FIFO")
		events = self.new_events(before)
		self.assertEqual(events[0]["event_type"], "provider_company_created")

	def test_create_provider_company_validation_and_conflict(self):
		frappe.set_user(self.operator)
		# Too short / invalid characters.
		provider_onboarding.create_provider_company(company_name="A")
		self.assertEqual(frappe.local.response.get("http_status_code"), 400)
		provider_onboarding.create_provider_company(company_name="Bad/Name!")
		self.assertEqual(frappe.local.response.get("http_status_code"), 400)
		# Already exists.
		provider_onboarding.create_provider_company(company_name=self.org)
		self.assertEqual(frappe.local.response.get("http_status_code"), 409)

	# -- admin onboarding ----------------------------------------------------

	def test_onboard_admin_new_user_creates_pending_invite(self):
		email = "admin.new@acme.example"
		frappe.set_user(self.operator)
		before = self.event_count()
		provider_onboarding.onboard_provider_admin(company=self.org, email=email, full_name="Ada Admin")
		self.assertEqual(frappe.local.response.get("status"), "success")
		data = frappe.local.response["data"]
		self.assertTrue(data["user_created"])
		self.assertTrue(data["invite_sent"])
		self.assertEqual(data["status"], "pending")
		self.assertTrue(frappe.db.exists("User", email))
		# No live membership or role until the invite is confirmed.
		self.assertFalse(frappe.get_all("Employee", filters={"user_id": email, "company": self.org}))
		self.assertNotIn("HISI Insurance Provider", frappe.get_roles(email))
		invite = self.pending_invite(email)
		self.assertEqual(len(invite), 1)
		self.assertEqual(invite[0]["role"], "HISI Insurance Provider")
		self.assertEqual(invite[0]["account_created"], 1)
		# The code is never the account's password (a placeholder is set).
		self.assertRaises(frappe.AuthenticationError, check_password, email, invite[0]["code"])
		# The styled email goes out with the provider wording.
		kw = self.mail_kwargs()
		self.assertEqual(kw.get("recipients"), [email])
		self.assertEqual(kw.get("template"), "officer_invite")
		self.assertEqual(kw["args"]["code"], invite[0]["code"])
		self.assertEqual(kw["args"]["new_account"], 1)
		self.assertEqual(kw["args"]["role"], "HISI Insurance Provider")
		self.assertIn("/verify-code", kw["args"]["sign_in_url"])
		events = self.new_events(before)
		self.assertEqual(events[0]["event_type"], "provider_admin_onboarded")

	def test_onboard_admin_existing_user_keeps_password(self):
		update_password(self.officer, "Existing-Pass-123")
		frappe.set_user(self.operator)
		provider_onboarding.onboard_provider_admin(company=self.org, email=self.officer)
		self.assertEqual(frappe.local.response.get("status"), "success")
		data = frappe.local.response["data"]
		self.assertFalse(data["user_created"])
		invite = self.pending_invite(self.officer)
		self.assertEqual(invite[0]["account_created"], 0)
		self.assertEqual(invite[0]["role"], "HISI Insurance Provider")
		self.assertEqual(check_password(self.officer, "Existing-Pass-123"), self.officer)

	def test_onboard_admin_validation_and_conflict(self):
		frappe.set_user(self.operator)
		provider_onboarding.onboard_provider_admin(company=self.org, email="not-an-email")
		self.assertEqual(frappe.local.response.get("http_status_code"), 400)
		provider_onboarding.onboard_provider_admin(company="Ghost Company", email="admin@acme.example")
		self.assertEqual(frappe.local.response.get("http_status_code"), 404)
		# Already an active member of the company.
		provider_onboarding.onboard_provider_admin(company=self.org, email=self.dispatcher)
		self.assertEqual(frappe.local.response.get("http_status_code"), 409)

	def test_onboard_admin_replaces_pending_invites_for_pair(self):
		email = "admin.swap@acme.example"
		# First as an officer (the regular dispatcher flow, so the invite lands
		# on the dispatcher's company), then as the admin via the console.
		frappe.set_user(self.dispatcher)
		onboarding.onboard_officer(email=email)
		officer_invite = self.pending_invite(email)
		frappe.set_user(self.operator)
		self.assertEqual(officer_invite[0]["role"], "HISI Insurance Surveillance")
		provider_onboarding.onboard_provider_admin(company=self.org, email=email)
		pending = frappe.get_all("HISI Officer Invite", filters={"email": email, "company": self.org}, fields=["name", "status", "role"])
		confirmed_pending = [row for row in pending if row["status"] == "Pending"]
		self.assertEqual(len(confirmed_pending), 1)
		self.assertEqual(confirmed_pending[0]["role"], "HISI Insurance Provider")
		self.assertEqual(pending[0]["status"] if len(pending) == 1 else [row["status"] for row in pending if row["name"] != confirmed_pending[0]["name"]][0], "Revoked")

	# -- verify (the one-time code) ------------------------------------------

	def test_verify_admin_invite_grants_provider_role_and_membership(self):
		email = "admin.flow@acme.example"
		frappe.set_user(self.operator)
		provider_onboarding.onboard_provider_admin(company=self.org, email=email)
		code = self.pending_invite(email)[0]["code"]
		# Wrong code: generic failure, account existence never disclosed.
		wrong = "000001" if code != "000001" else "000002"
		with self.assertRaises(frappe.ValidationError):
			onboarding.verify_invite_code(email=email, code=wrong)
		out = onboarding.verify_invite_code(email=email, code=code)
		self.assertTrue(out["verified"])
		self.assertEqual(out["account"], "new")
		self.assertTrue(out["key"])
		# The invite is consumed.
		row = frappe.get_all("HISI Officer Invite", filters={"email": email}, fields=["status"], order_by="creation desc", limit=1)[0]
		self.assertEqual(row["status"], "Confirmed")
		# Membership is live with the provider designation, and the role is granted.
		employees = frappe.get_all("Employee", filters={"user_id": email, "company": self.org, "status": "Active"}, fields=["designation"])
		self.assertEqual(len(employees), 1)
		self.assertEqual(employees[0]["designation"], "HISI Insurance Provider")
		self.assertIn("HISI Insurance Provider", frappe.get_roles(email))

	def test_verify_admin_invite_existing_account_signs_in(self):
		update_password(self.officer, "Existing-Pass-123")
		frappe.set_user(self.operator)
		provider_onboarding.onboard_provider_admin(company=self.org, email=self.officer)
		code = self.pending_invite(self.officer)[0]["code"]
		out = onboarding.verify_invite_code(email=self.officer, code=code)
		self.assertTrue(out["verified"])
		self.assertEqual(out["account"], "existing")
		self.assertIsNone(out.get("key"))
		# The confirmed invite is a one-time sign-in grant.
		self.assertEqual(frappe.session.user, self.officer)
		self.assertIn("HISI Insurance Provider", frappe.get_roles(self.officer))

	def test_officer_invite_defaults_to_officer_role(self):
		"""Backwards compatibility: the regular officer flow keeps the
		surveillance role (the new field defaults to it)."""
		email = "officer.default@acme.example"
		frappe.set_user(self.dispatcher)
		onboarding.onboard_officer(email=email)
		invite = self.pending_invite(email)
		self.assertEqual(invite[0]["role"], "HISI Insurance Surveillance")
