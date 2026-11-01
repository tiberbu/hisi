import json
from unittest.mock import patch

import frappe
from frappe.auth import CookieManager, LoginManager
from frappe.utils.password import check_password, update_password

from hisi.api.auth import complete_password_reset
from hisi.api.surveillance import onboarding
from hisi.tests.test_surveillance import SurveillanceTestCase


class _RequestStub:
	"""Minimal stand-in for the werkzeug Request in the test runner.

	Attribute access works (Session/LoginManager read .path/.cookies), but
	the stub is falsy, so request-presence gates (rate limiter, CSRF) behave
	as if there were no request — matching the rest of the suite.
	"""

	path = "/tests"
	method = "GET"
	scheme = "http"
	cookies = {}
	headers = {}

	def __bool__(self):
		return False


class TestOfficerOnboarding(SurveillanceTestCase):
	"""Officer invite flow: a dispatcher sends a one-time code; new accounts
	exchange it for a first password, existing accounts confirm the company
	link (and are signed in once). Membership stays pending until confirmed."""

	def setUp(self):
		super().setUp()
		# The onboarding flow signs users in through frappe.local.login_manager,
		# which only exists on real HTTP requests (RequestContext.set_session);
		# emulate the request context so login_as() works under the test runner.
		if not hasattr(frappe.local, "request"):
			frappe.local.request = _RequestStub()
		frappe.local.cookie_manager = CookieManager()
		frappe.local.login_manager = LoginManager()
		# Mock sendmail so the invite flow records mail calls for assertions
		# without real delivery. (Commit neutralisation lives in the base
		# SurveillanceTestCase, which keeps the per-test savepoint intact.)
		self.mail_patcher = patch("frappe.sendmail")
		self.sent_mail = self.mail_patcher.start()

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
			fields=["name", "code", "company", "account_created"],
			order_by="creation desc",
			limit=1,
		)

	def wrong_code_for(self, real_code):
		return "000001" if real_code != "000001" else "000002"

	# -- listing ------------------------------------------------------------

	def test_list_members_shape_and_audit(self):
		frappe.set_user(self.dispatcher)
		before = self.event_count()
		onboarding.list_payer_members()
		self.assertEqual(frappe.local.response.get("status"), "success")
		data = frappe.local.response["data"]
		self.assertEqual(data["total"], 1)
		row = data["members"][0]
		self.assertEqual(row["user"], self.dispatcher)
		self.assertEqual(row["status"], "active")
		self.assertIsInstance(row["id"], str)
		self.assertTrue(any(org["company"] == self.org for org in data["companies"]))
		acme = next(org for org in data["companies"] if org["company"] == self.org)
		self.assertEqual(acme["active_members"], 1)
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "members_listed")

	def test_list_members_cross_payer_sees_none(self):
		frappe.set_user(self.dispatcher_b)
		onboarding.list_payer_members()
		self.assertEqual(frappe.local.response.get("status"), "success")
		data = frappe.local.response["data"]
		self.assertEqual(data["total"], 1)
		self.assertEqual(data["members"][0]["user"], self.dispatcher_b)
		self.assertEqual(data["members"][0]["company"], self.org_b)
		self.assertNotIn(self.org, [org["company"] for org in data["companies"]])

	def test_list_shows_pending_and_active_rows(self):
		email = "pending.row@acme.example"
		frappe.set_user(self.dispatcher)
		onboarding.onboard_officer(email=email)
		onboarding.list_payer_members()
		data = frappe.local.response["data"]
		by_user = {member["user"]: member for member in data["members"]}
		self.assertEqual(by_user[email]["status"], "pending")
		self.assertEqual(by_user[self.dispatcher]["status"], "active")
		self.assertEqual(data["total"], 2)
		self.assertTrue(all(isinstance(member["id"], str) for member in data["members"]))

	# -- onboarding ----------------------------------------------------------

	def test_onboard_new_user_creates_pending_invite(self):
		email = "new.officer@acme.example"
		frappe.set_user(self.dispatcher)
		before = self.event_count()
		onboarding.onboard_officer(email=email, full_name="New Officer")
		self.assertEqual(frappe.local.response.get("status"), "success")
		data = frappe.local.response["data"]
		self.assertTrue(data["user_created"])
		self.assertTrue(data["invite_sent"])
		self.assertEqual(data["status"], "pending")
		self.assertEqual(data["company"], self.org)
		self.assertTrue(frappe.db.exists("User", email))
		# No live membership and no role until the invite is confirmed.
		self.assertFalse(frappe.get_all("Employee", filters={"user_id": email, "company": self.org_company(self.org)}))
		self.assertNotIn("HISI Insurance Surveillance", frappe.get_roles(email))
		invite = self.pending_invite(email)
		self.assertEqual(len(invite), 1)
		self.assertEqual(invite[0]["company"], self.org)
		self.assertEqual(invite[0]["account_created"], 1)
		code = invite[0]["code"]
		# The code is never the account's password (a placeholder is set);
		# check_password raises on mismatch.
		self.assertRaises(frappe.AuthenticationError, check_password, email, code)
		# The styled email goes out with the code and the app sign-in link.
		kw = self.mail_kwargs()
		self.assertEqual(kw.get("recipients"), [email])
		self.assertEqual(kw.get("template"), "officer_invite")
		self.assertEqual(kw["args"]["code"], code)
		self.assertEqual(kw["args"]["new_account"], 1)
		self.assertIn("/verify-code", kw["args"]["sign_in_url"])
		self.assertNotIn(code, json.dumps(frappe.local.response))
		events = self.new_events(before)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event_type"], "officer_onboarded")
		self.assertEqual(events[0]["target_type"], "user")

	def test_onboard_existing_user_sends_invite_keeps_password(self):
		update_password(self.officer, "Existing-Pass-123")
		frappe.set_user(self.dispatcher)
		onboarding.onboard_officer(email=self.officer)
		self.assertEqual(frappe.local.response.get("status"), "success")
		data = frappe.local.response["data"]
		self.assertFalse(data["user_created"])
		self.assertTrue(data["invite_sent"])
		self.assertEqual(data["status"], "pending")
		# The invite exists but the membership is not live yet.
		self.assertFalse(
			frappe.get_all("Employee", filters={"user_id": self.officer, "company": self.org_company(self.org), "status": "Active"})
		)
		invite = self.pending_invite(self.officer)
		self.assertEqual(invite[0]["account_created"], 0)
		kw = self.mail_kwargs()
		self.assertEqual(kw.get("recipients"), [self.officer])
		self.assertEqual(kw["args"]["code"], invite[0]["code"])
		self.assertEqual(kw["args"]["new_account"], 0)
		# The existing password is untouched.
		self.assertEqual(check_password(self.officer, "Existing-Pass-123"), self.officer)

	def test_onboard_duplicate_member_conflicts(self):
		frappe.set_user(self.dispatcher)
		onboarding.onboard_officer(email=self.dispatcher)
		self.assertEqual(frappe.local.response.get("status"), "error")
		self.assertEqual(frappe.local.response.get("http_status_code"), 409)

	# -- verify (the one-time code) ------------------------------------------

	def test_verify_new_user_exchanges_code_for_first_password(self):
		email = "flow.new@acme.example"
		frappe.set_user(self.dispatcher)
		onboarding.onboard_officer(email=email, full_name="Flow New")
		code = self.pending_invite(email)[0]["code"]
		# Wrong code, unknown account, malformed code: same generic failure.
		self.assertRaises(frappe.ValidationError, onboarding.verify_invite_code, email=email, code=self.wrong_code_for(code))
		self.assertRaises(frappe.ValidationError, onboarding.verify_invite_code, email="ghost@nowhere.example", code="123456")
		self.assertRaises(frappe.ValidationError, onboarding.verify_invite_code, email=email, code="abcdef")
		out = onboarding.verify_invite_code(email=email, code=code)
		self.assertTrue(out["verified"])
		self.assertEqual(out["account"], "new")
		self.assertTrue(out["key"])
		# The invite is consumed.
		row = frappe.get_all("HISI Officer Invite", filters={"email": email}, fields=["status"], order_by="creation desc", limit=1)[0]
		self.assertEqual(row["status"], "Confirmed")
		# The membership is now live.
		rows = frappe.get_all("Employee", filters={"user_id": email, "company": self.org_company(self.org)}, fields=["status", "designation"])
		self.assertEqual(rows[0]["status"], "Active")
		self.assertEqual(rows[0]["designation"], "HISI Insurance Surveillance")
		self.assertIn("HISI Insurance Surveillance", frappe.get_roles(email))
		# The returned key sets the first password (and signs the user in).
		complete_password_reset(key=out["key"], new_password="First-Pass-123")
		self.assertEqual(check_password(email, "First-Pass-123"), email)
		# check_password raises on mismatch.
		self.assertRaises(frappe.AuthenticationError, check_password, email, code)
		self.assertEqual(frappe.db.get_value("User", email, "reset_password_key") or "", "")
		# The code cannot be reused.
		self.assertRaises(frappe.ValidationError, onboarding.verify_invite_code, email=email, code=code)

	def test_verify_existing_user_confirms_link_and_signs_in(self):
		update_password(self.officer, "Existing-Pass-123")
		frappe.set_user(self.dispatcher)
		onboarding.onboard_officer(email=self.officer)
		code = self.pending_invite(self.officer)[0]["code"]
		out = onboarding.verify_invite_code(email=self.officer, code=code)
		self.assertTrue(out["verified"])
		self.assertEqual(out["account"], "existing")
		self.assertNotIn("key", out)
		# One-time sign-in grant.
		self.assertEqual(frappe.local.login_manager.user, self.officer)
		# The real password is untouched and the membership is live.
		self.assertEqual(check_password(self.officer, "Existing-Pass-123"), self.officer)
		rows = frappe.get_all("Employee", filters={"user_id": self.officer, "company": self.org_company(self.org)}, fields=["status"])
		self.assertEqual(rows[0]["status"], "Active")
		# The code cannot be reused.
		self.assertRaises(frappe.ValidationError, onboarding.verify_invite_code, email=self.officer, code=code)

	def test_verify_expired_invite_rejected(self):
		email = "expired@acme.example"
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Expired", "user_type": "System User", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "HISI Officer Invite",
				"email": email,
				"company": self.org,
				"code": "424242",
				"status": "Pending",
				"expires_at": "2020-01-01 00:00:00",
			}
		).insert(ignore_permissions=True)
		self.assertRaises(frappe.ValidationError, onboarding.verify_invite_code, email=email, code="424242")
		row = frappe.get_all("HISI Officer Invite", filters={"email": email}, fields=["status"], limit=1)[0]
		self.assertEqual(row["status"], "Pending")

	# -- resend ---------------------------------------------------------------

	def test_resend_rotates_code(self):
		email = "resent@acme.example"
		frappe.set_user(self.dispatcher)
		onboarding.onboard_officer(email=email)
		code1 = self.pending_invite(email)[0]["code"]
		onboarding.resend_officer_invite(email=email)
		self.assertEqual(frappe.local.response.get("status"), "success")
		code2 = self.pending_invite(email)[0]["code"]
		self.assertNotEqual(code1, code2)
		states = [row["status"] for row in frappe.get_all("HISI Officer Invite", filters={"email": email}, fields=["status"], order_by="creation asc")]
		self.assertEqual(states, ["Revoked", "Pending"])
		self.assertEqual(self.sent_mail.call_count, 2)
		# The rotated (revoked) code no longer works; the fresh one does.
		self.assertRaises(frappe.ValidationError, onboarding.verify_invite_code, email=email, code=code1)
		out = onboarding.verify_invite_code(email=email, code=code2)
		self.assertEqual(out["account"], "new")

	def test_resend_requires_pending_invite(self):
		frappe.set_user(self.dispatcher)
		# No account at all.
		onboarding.resend_officer_invite(email="ghost@nowhere.example")
		self.assertEqual(frappe.local.response.get("http_status_code"), 404)
		# An account, but no pending invite for the organization.
		onboarding.resend_officer_invite(email=self.officer)
		self.assertEqual(frappe.local.response.get("http_status_code"), 404)
		# An already-active member.
		onboarding.resend_officer_invite(email=self.dispatcher)
		self.assertEqual(frappe.local.response.get("http_status_code"), 409)

	# -- update / remove --------------------------------------------------------

	def test_update_officer_renames_member(self):
		email = "renamed@acme.example"
		frappe.set_user(self.dispatcher)
		onboarding.onboard_officer(email=email, full_name="Original Name")
		onboarding.update_officer(email=email, full_name="Renamed Officer")
		self.assertEqual(frappe.local.response.get("status"), "success")
		self.assertEqual(frappe.local.response["data"]["full_name"], "Renamed Officer")
		user = frappe.get_doc("User", email)
		self.assertEqual(user.first_name, "Renamed")
		self.assertEqual(user.last_name, "Officer")
		# A dispatcher of another organization cannot rename this member.
		frappe.set_user(self.dispatcher_b)
		onboarding.update_officer(email=email, full_name="Nope")
		self.assertEqual(frappe.local.response.get("http_status_code"), 404)

	def test_remove_officer_revokes_pending_invite(self):
		email = "removed@acme.example"
		frappe.set_user(self.dispatcher)
		onboarding.onboard_officer(email=email)
		code = self.pending_invite(email)[0]["code"]
		onboarding.remove_officer(email=email)
		self.assertEqual(frappe.local.response.get("status"), "success")
		states = [row["status"] for row in frappe.get_all("HISI Officer Invite", filters={"email": email}, fields=["status"])]
		self.assertEqual(states, ["Revoked"])
		self.assertFalse(frappe.get_all("Employee", filters={"user_id": email, "company": self.org_company(self.org)}))
		self.assertRaises(frappe.ValidationError, onboarding.verify_invite_code, email=email, code=code)
