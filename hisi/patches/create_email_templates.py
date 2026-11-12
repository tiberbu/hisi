"""Seed the HISI transactional Email Template records.

All HISI transactional email (sign-in codes, officer invites, password
resets) is stored as **Email Template** documents so the wording, branding
and subjects are editable from the Desk — no HTML or addresses hardcoded in
Python. This patch seeds the three records on every site (create-or-update
when the HTML still matches the shipped default, so desk edits survive
re-runs).

Subjects live on the template document too; the senders come from the
default outgoing **Email Account** at send time (see
``hisi.utils.get_outgoing_sender``).
"""

import frappe
from hisi import brand

LOGIN_CODE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<title>Your one-time sign-in code for {{ brand }}</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f7f5;-webkit-text-size-adjust:100%;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f7f5;padding:40px 0;">
<tr>
<td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:100%;background-color:#ffffff;border-radius:16px;border:1px solid #e5edea;">
  <tr>
    <td style="background-color:#1a6b48;padding:22px 44px;">
      <span style="display:inline-block;color:#ffffff;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:20px;font-weight:600;letter-spacing:1px;line-height:1.2;">{{ brand }}</span>
      <span style="display:inline-block;margin-left:12px;color:#adebb3;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:11px;font-weight:500;letter-spacing:1.2px;text-transform:uppercase;line-height:1.8;">Sign-in</span>
    </td>
  </tr>
  <tr>
    <td style="padding:36px 44px 8px;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:15px;line-height:1.65;color:#374151;">
      <p style="margin:0 0 16px;font-size:18px;font-weight:600;color:#111827;">Hello {{ first_name }},</p>
      {% if resend %}
      <p style="margin:0;">Here is a fresh one-time code to finish signing in to {{ brand }}. Any earlier code sent to this address has been cancelled and will no longer work.</p>
      {% else %}
      <p style="margin:0;">To finish signing in to {{ brand }}, enter the one-time code below on the sign-in page.</p>
      {% endif %}
    </td>
  </tr>
  <tr>
    <td align="center" style="padding:20px 44px 8px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%;background-color:#f0f9f3;border:1px solid #d7e5dd;border-radius:12px;">
      <tr>
        <td align="center" style="padding:22px 16px;">
          <p style="margin:0 0 6px;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:12px;font-weight:500;letter-spacing:1.5px;text-transform:uppercase;color:#1a6b48;">Your one-time code</p>
          <p style="margin:0;font-family:'IBM Plex Mono','Courier New',monospace;font-size:36px;font-weight:600;letter-spacing:12px;text-indent:12px;color:#1a6b48;">{{ code }}</p>
        </td>
      </tr>
      </table>
    </td>
  </tr>
  <tr>
    <td align="center" style="padding:24px 44px 8px;">
      <a href="{{ sign_in_url }}" style="display:inline-block;background-color:#1a6b48;color:#ffffff;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:15px;font-weight:600;letter-spacing:0.3px;text-decoration:none;padding:14px 34px;border-radius:10px;">Open sign-in</a>
    </td>
  </tr>
  <tr>
    <td style="padding:20px 44px 32px;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:13px;line-height:1.6;color:#6b7280;">
      <p style="margin:0 0 16px;">This code works once and expires in {{ expires_minutes }} minutes. If you did not request a sign-in code, you can safely ignore this email — your account is not affected.</p>
      <p style="margin:0;">Thank you,<br><strong style="color:#1a6b48;">{{ brand }}</strong></p>
    </td>
  </tr>
  <tr>
    <td style="background-color:#f8faf9;border-top:1px solid #edf2ef;padding:14px 44px;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:11.5px;color:#9ca3af;">{{ brand_company }} · {{ brand }}</td>
  </tr>
</table>
</td>
</tr>
</table>
</body>
</html>"""

OFFICER_INVITE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<title>Your {{ brand }} invitation</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f7f5;-webkit-text-size-adjust:100%;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f7f5;padding:40px 0;">
<tr>
<td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:100%;background-color:#ffffff;border-radius:16px;border:1px solid #e5edea;">
  <tr>
    <td style="background-color:#1a6b48;padding:22px 44px;">
      <span style="display:inline-block;color:#ffffff;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:20px;font-weight:600;letter-spacing:1px;line-height:1.2;">{{ brand }}</span>
      <span style="display:inline-block;margin-left:12px;color:#adebb3;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:11px;font-weight:500;letter-spacing:1.2px;text-transform:uppercase;line-height:1.8;">Surveillance platform</span>
    </td>
  </tr>
  <tr>
    <td style="padding:36px 44px 8px;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:15px;line-height:1.65;color:#374151;">
      <p style="margin:0 0 16px;font-size:18px;font-weight:600;color:#111827;">Hello {{ first_name }},</p>
      {% if role == 'HISI Insurance Provider' %}
      {% if new_account %}
      <p style="margin:0 0 12px;">Your account at <strong style="color:#1a6b48;">{{ company }}</strong> on {{ brand }} is ready.</p>
      <p style="margin:0;">You are set up as the organization's administrator. To get started, enter the one-time code below on the sign-in page and choose a password — you will be signed in as soon as it is saved. From the Team page you can then invite your members to the surveillance platform.</p>
      {% else %}
      <p style="margin:0 0 12px;">You have been added to <strong style="color:#1a6b48;">{{ company }}</strong> on {{ brand }} as an administrator.</p>
      <p style="margin:0;">Enter the one-time code below to confirm the invitation. You will be signed in straight away, and can invite your members to the surveillance platform from the Team page.</p>
      {% endif %}
      {% else %}
      {% if new_account %}
      <p style="margin:0 0 12px;">Your account at <strong style="color:#1a6b48;">{{ company }}</strong> on {{ brand }} is ready.</p>
      <p style="margin:0;">You are set up as a surveillance officer. To get started, enter the one-time code below on the sign-in page and choose a password — you will be signed in as soon as it is saved.</p>
      {% else %}
      <p style="margin:0 0 12px;">You have been added to <strong style="color:#1a6b48;">{{ company }}</strong> on {{ brand }} as a surveillance officer.</p>
      <p style="margin:0;">Enter the one-time code below to confirm the invitation. You will be signed in straight away, with the new organization ready to use.</p>
      {% endif %}
      {% endif %}
    </td>
  </tr>
  <tr>
    <td align="center" style="padding:20px 44px 8px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%;background-color:#f0f9f3;border:1px solid #d7e5dd;border-radius:12px;">
      <tr>
        <td align="center" style="padding:22px 16px;">
          <p style="margin:0 0 6px;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:12px;font-weight:500;letter-spacing:1.5px;text-transform:uppercase;color:#1a6b48;">Your one-time code</p>
          <p style="margin:0;font-family:'IBM Plex Mono','Courier New',monospace;font-size:36px;font-weight:600;letter-spacing:12px;text-indent:12px;color:#1a6b48;">{{ code }}</p>
        </td>
      </tr>
      </table>
    </td>
  </tr>
  <tr>
    <td align="center" style="padding:24px 44px 8px;">
      <a href="{{ sign_in_url }}" style="display:inline-block;background-color:#1a6b48;color:#ffffff;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:15px;font-weight:600;letter-spacing:0.3px;text-decoration:none;padding:14px 34px;border-radius:10px;">Enter your code</a>
    </td>
  </tr>
  <tr>
    <td style="padding:20px 44px 32px;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:13px;line-height:1.6;color:#6b7280;">
      {% if new_account %}
      <p style="margin:0 0 16px;">This code works once and is only for your first sign-in — once you set a password, it stops working.</p>
      {% else %}
      <p style="margin:0 0 16px;">This code works once, for this invitation only. It cannot be used to change your password.</p>
      {% endif %}
      <p style="margin:0;">Thank you,<br><strong style="color:#1a6b48;">{{ brand }}</strong></p>
    </td>
  </tr>
  <tr>
    <td style="background-color:#f8faf9;border-top:1px solid #edf2ef;padding:14px 44px;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:11.5px;color:#9ca3af;">{{ brand_company }} · {{ brand }}</td>
  </tr>
</table>
</td>
</tr>
</table>
</body>
</html>"""

FORGOT_PASSWORD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<title>Reset your {{ brand }} password</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f7f5;-webkit-text-size-adjust:100%;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f7f5;padding:40px 0;">
<tr>
<td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:100%;background-color:#ffffff;border-radius:16px;border:1px solid #e5edea;">
  <tr>
    <td style="background-color:#1a6b48;padding:22px 44px;">
      <span style="display:inline-block;color:#ffffff;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:20px;font-weight:600;letter-spacing:1px;line-height:1.2;">{{ brand }}</span>
      <span style="display:inline-block;margin-left:12px;color:#adebb3;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:11px;font-weight:500;letter-spacing:1.2px;text-transform:uppercase;line-height:1.8;">Security</span>
    </td>
  </tr>
  <tr>
    <td style="padding:36px 44px 8px;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:15px;line-height:1.65;color:#374151;">
      <p style="margin:0 0 16px;font-size:18px;font-weight:600;color:#111827;">Hello {{ first_name }},</p>
      <p style="margin:0;">We received a request to reset the password for your {{ brand }} account. Click the button below to choose a new password.</p>
    </td>
  </tr>
  <tr>
    <td align="center" style="padding:24px 44px 8px;">
      <a href="{{ link }}" style="display:inline-block;background-color:#1a6b48;color:#ffffff;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:15px;font-weight:600;letter-spacing:0.3px;text-decoration:none;padding:14px 34px;border-radius:10px;">Set a new password</a>
    </td>
  </tr>
  <tr>
    <td style="padding:20px 44px 32px;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:13px;line-height:1.6;color:#6b7280;">
      <p style="margin:0 0 16px;">This link works once. If you did not request a password reset, you can safely ignore this email — your account is not affected.</p>
      <p style="margin:0;">Thank you,<br><strong style="color:#1a6b48;">{{ brand }}</strong></p>
    </td>
  </tr>
  <tr>
    <td style="background-color:#f8faf9;border-top:1px solid #edf2ef;padding:14px 44px;font-family:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;font-size:11.5px;color:#9ca3af;">{{ brand_company }} · {{ brand }}</td>
  </tr>
</table>
</td>
</tr>
</table>
</body>
</html>"""

#: Subjects are plain text on the record (rendered once at seed time), so
#: they read the brand from the single source directly.
TEMPLATES = {
	"HISI Login Code": (f"Your one-time sign-in code for {brand.BRAND_NAME}", LOGIN_CODE_HTML),
	"HISI Officer Invite": (f"Your {brand.BRAND_NAME} invitation", OFFICER_INVITE_HTML),
	"HISI Forgot Password": (f"Reset your {brand.BRAND_NAME} password", FORGOT_PASSWORD_HTML),
}


def execute():
	"""Create the Email Template records (idempotent: skipped when present).

	Records are seeded once; afterwards the Desk is the source of truth and
	re-runs never overwrite operator edits.
	"""
	for name, (subject, html) in TEMPLATES.items():
		if frappe.db.exists("Email Template", name):
			# Repair half-seeded records (empty body) without touching edits.
			if not frappe.db.get_value("Email Template", name, "response_html"):
				frappe.db.set_value("Email Template", name, {"use_html": 1, "response_html": html})
			continue
		frappe.get_doc(
			{
				"doctype": "Email Template",
				"name": name,
				"subject": subject,
				"use_html": 1,
				"response_html": html,
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()
