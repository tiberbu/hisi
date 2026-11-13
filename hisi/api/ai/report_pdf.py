# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""PDF rendering for the ``HISI Discrepancy Report`` job.

The deliverable of a completed run is a PDF: :func:`render_report_html`
builds a self-contained page — a self-branded masthead (dark brand bar with the
logo mark; no product naming), a document title row (claim-scoped reports read as a
"Payment Decision Report"), a subject/context block (member, CR ID, claim,
claim status, generated), a verdict band with severity counts, the
executive summary, one severity-railed block per finding with its evidence,
and a footer — and :func:`build_report_pdf_bytes` renders it with
pdfkit/wkhtmltopdf.

The wkhtmltopdf engine is an old WebKit, so the markup is deliberately
plain: block/table layout, floats only for the finding index, lists and
inline-block chips — no flexbox/grid/gradients. Palette mirrors the app
theme (see ``apps/hisi_app/lib/brand.ts``).

The page is fully self-contained: the logo is inlined as a base64 data URI
(wkhtmltopdf does not resolve relative file paths from the stdin HTML), so
the PDF renders the brand mark without any temp files or file:// URLs.
"""

import base64
import os

import frappe
from frappe.utils.pdf import pdfkit

#: The report logo (same mark the frontend brand.ts serves). Kept beside the
#: module and inlined so the PDF build is self-contained (wkhtmltopdf does not
#: resolve relative file paths from the stdin HTML).
LOGO_PATH = os.path.join(os.path.dirname(__file__), "report_logo.jpeg")
LOGO_DATA_URI = None  # cached on first use

SEVERITIES = ("low", "medium", "high")

_SEVERITY_COLOR = {
	"high": "#dc2626",
	"medium": "#d97706",
	"low": "#0284c7",
}
_SEVERITY_TINT = {
	"high": "#fef2f2",
	"medium": "#fffbeb",
	"low": "#f0f9ff",
}
_SEVERITY_ORDER = {
	"high": 0,
	"medium": 1,
	"low": 2,
}

#: The overall verdict -> (label, css class) for the masthead verdict chip.
VERDICT_LABEL = {
	"clean": ("No material discrepancies", "ok"),
	"minor": ("Minor issues found", "minor"),
	"major": ("Major issues found", "major"),
}
VERDICT_CLASS = {
	"ok": ("#166534", "#ecfdf3"),
	"minor": ("#92400e", "#fffbeb"),
	"major": ("#991b1b", "#fef2f2"),
}


def _logo_data_uri() -> str:
	"""Inline the logo as a base64 data URI, or an empty string on any failure
	(the report must render even if the asset is missing)."""
	global LOGO_DATA_URI
	if LOGO_DATA_URI is not None:
		return LOGO_DATA_URI
	try:
		if os.path.exists(LOGO_PATH) and os.path.getsize(LOGO_PATH) > 0:
			with open(LOGO_PATH, "rb") as f:
				LOGO_DATA_URI = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode("ascii")
			return LOGO_DATA_URI
	except Exception:
		pass
	LOGO_DATA_URI = ""
	return ""


def _esc(value) -> str:
	return (
		str(value)
		.replace("&", "&amp;")
		.replace("<", "&lt;")
		.replace(">", "&gt;")
		.replace('"', "&quot;")
	)


def _chip(severity: str, text: str) -> str:
	color = _SEVERITY_COLOR.get(severity, "#6b7280")
	tint = _SEVERITY_TINT.get(severity, "#f9fafb")
	return (
		f'<span style="display:inline-block;padding:3px 10px;border-radius:999px;'
		f'font-size:12px;font-weight:700;letter-spacing:.02em;color:{color};background:{tint};'
		f'border:1px solid {color}33;">{text}</span>'
	)


def _stat_cell(label: str, count: int, severity: str | None) -> str:
	if severity is None:
		color, tint = "#111827", "#f9fafb"
	else:
		color = _SEVERITY_COLOR[severity]
		tint = _SEVERITY_TINT[severity]
	return (
		'<td style="width:33%;padding:12px 14px;border:1px solid #e5e7eb;border-top:3px solid '
		f'{color};background:{tint};">'
		f'<div style="font-size:26px;font-weight:800;color:{color};">{count}</div>'
		f'<div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#6b7280;font-weight:600;">'
		f"{label}</div></td>"
	)


def _format_duration(value) -> str | None:
	"""Human-friendly duration from the worker's ``duration_ms`` callable or
	its stored int (``None``/missing -> hidden, never an error)."""
	if value is None:
		return None
	if callable(value):
		try:
			value = value()
		except Exception:
			return None
	if isinstance(value, (int, float)) and value > 0:
		return f"{value / 1000.0:.1f}s"
	return None


def _section_label(code: str | None) -> str:
	"""Human label for a finding's tag code (falls back to the code itself)."""
	label = (code or "").strip()
	if not label:
		return "Finding"
	if label.isupper() and label.isalnum():
		return label.replace("_", " ").title()
	return label


def _finding_block(idx: int, f: dict) -> str:
	severity = str(f.get("severity") or "medium").lower()
	color = _SEVERITY_COLOR.get(severity, "#6b7280")
	tint = _SEVERITY_TINT.get(severity, "#f9fafb")
	title = f.get("title") or "Finding"
	detail = f.get("detail") or ""
	section = _section_label(f.get("tag_code"))
	evidence = f.get("evidence") or []

	evidence_html = ""
	if evidence:
		items = "".join(f"<li>{_esc(e)}</li>" for e in evidence)
		evidence_html = (
			'<div style="margin-top:8px;font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#9ca3af;">'
			"Evidence</div>"
			f'<ul style="margin:4px 0 0 18px;padding:0;color:#4b5563;font-size:12px;line-height:1.5;">{items}</ul>'
		)

	return (
		'<div style="border:1px solid #e5e7eb;border-left:4px solid '
		f"{color};border-radius:8px;padding:14px 16px;margin-bottom:12px;background:#ffffff;page-break-inside:avoid;\">"
		'<div style="float:right;">' + _chip(severity, severity.upper()) + "</div>"
		f'<div style="font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#9ca3af;">'
		f"{idx}. {_esc(section)}</div>"
		f'<div style="font-size:15px;font-weight:700;color:#111827;margin-top:2px;">{_esc(title)}</div>'
		f'<div style="font-size:13px;color:#374151;line-height:1.55;margin-top:4px;">{_esc(detail)}</div>'
		f"{evidence_html}"
		'<div style="clear:both;"></div>'
		"</div>"
	)


def render_report_html(
	cr_id: str,
	subject_name: str,
	result: dict,
	generated_at: str | None = None,
	duration_ms=None,
	data_sources: str | None = None,
	report_ref: str | None = None,
	subject_claim: str | None = None,
	subject_visit: str | None = None,
	subject_preauth: str | None = None,
	claim_status: str | None = None,
	claim_status_label: str | None = None,
) -> str:
	"""The full report page. ``result`` is the normalized payload
	(``summary`` / ``executive_summary`` / ``overall`` / ``findings``);
	duration_ms, data_sources and report_ref (all optional, from the job row)
	enrich the masthead context block and the footer.

	``subject_claim`` + ``claim_status`` (both optional) mark the report as a
	claim-scoped payment-decision report and surface the claim's dispatch
	state in the context block (a claim that was not sent to the payer is
	flagged, so the findings below it are read in the right context)."""
	result = result or {}
	findings = sorted(
		(f for f in (result.get("findings") or []) if isinstance(f, dict)),
		key=lambda x: (
			_SEVERITY_ORDER.get(str(x.get("severity") or "medium").lower(), 3),
			str(x.get("tag_code") or ""),
			str(x.get("title") or ""),
		),
	)
	counts = {s: 0 for s in SEVERITIES}
	for f in findings:
		s = str(f.get("severity") or "medium").lower()
		if s in counts:
			counts[s] += 1

	overall = str(result.get("overall") or "minor").lower()
	verdict_text, vkey = VERDICT_LABEL.get(overall, VERDICT_LABEL["minor"])
	vfg, vbg = VERDICT_CLASS[vkey]

	claim_scope = bool((subject_claim or "").strip())
	visit_scope = bool((subject_visit or "").strip())
	preauth_scope = bool((subject_preauth or "").strip())
	if claim_scope:
		doc_title = "Payment Decision Report"
		subtitle = f"Claim-level review for the payment decision — claim {_esc(subject_claim.strip())}"
	elif visit_scope:
		doc_title = "Discrepancy Report"
		subtitle = f"Visit-level review for the payment decision — visit {_esc(subject_visit.strip())}"
	elif preauth_scope:
		doc_title = "Discrepancy Report"
		subtitle = f"Preauthorization review — preauth {_esc(subject_preauth.strip())}"
	else:
		doc_title = "Discrepancy Report"
		subtitle = "Automated discrepancy review"

	# Claim dispatch state: prefer the explicit status passed in; flag the
	# context row when the claim was not successfully sent to the payer.
	status_value = str(claim_status or "").strip()
	status_label = str(claim_status_label or "").strip()
	status_text = status_label or status_value
	not_dispatched = any(
		str(f.get("tag_code") or "").strip().upper() == "CLAIM_NOT_DISPATCHED" for f in findings
	)
	if not_dispatched and status_text:
		status_html = (
			'<span style="color:#991b1b;font-weight:700;">' + _esc(status_text) + "</span>"
			'<br><span style="font-size:11px;font-weight:700;color:#991b1b;letter-spacing:.04em;">NOT SENT TO PAYER</span>'
		)
	elif status_text:
		status_html = _esc(status_text) if status_label else _esc(status_value.upper())
	else:
		status_html = "&mdash;"

	# ---- masthead ----------------------------------------------------------
	logo_uri = _logo_data_uri()
	if logo_uri:
		# The brand mark is 2:1 with its own white background (the plate).
		# Vendored pre-resized to 84x42 — this wkhtmltopdf build scales the
		# bitmap by layout context (size attributes are ignored), so it lands
		# at ~64x32px in the masthead: compact, flush, no extra card.
		logo_html = f'<img src="{logo_uri}" style="display:block;">'
	else:
		logo_html = (
			'<div style="width:32px;height:32px;border-radius:6px;background:#ffffff22;'
			'display:table-cell;text-align:center;line-height:32px;color:#ffffff;font-weight:800;font-size:16px;">H</div>'
		)

	brand_bar = (
		'<div style="background:#15803d;margin:-13.5px -13.5px 0 -13.5px;padding:10px 41px;">'
		"<table width='100%' cellpadding='0' cellspacing='0'><tr>"
		"<td style='vertical-align:middle;'>"
		'<table cellpadding="0" cellspacing="0"><tr>'
		f"<td style='padding-right:12px;'>{logo_html}</td>"
		"<td style='vertical-align:middle;'>"
		'<div style="font-size:10px;color:#bbf7d0;letter-spacing:.14em;text-transform:uppercase;">Discrepancy Review</div>'
		"</td></tr></table>"
		"</td>"
		"<td style='vertical-align:middle;text-align:right;'>"
		'<div style="font-size:10px;color:#bbf7d0;letter-spacing:.18em;text-transform:uppercase;font-weight:700;">Confidential &mdash; internal review</div>'
		"</td>"
		"</tr></table>"
		"</div>"
	)

	def _cell(label: str, value: str, bg: str, width: str) -> str:
		return (
			'<td style="padding:10px 16px;border:1px solid #e5e7eb;background:' + bg + ";width:" + width + ';">'
			'<div style="font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#9ca3af;font-weight:700;">'
			+ label
			+ '</div><div style="font-size:14px;font-weight:700;color:#111827;margin-top:2px;word-break:break-word;">'
			+ value
			+ "</div></td>"
		)

	generated = str(generated_at or "")[:16].replace("T", " ")
	analysis = _format_duration(duration_ms)
	sources_text = _esc(data_sources or "patient, claims, preauth")
	if claim_scope:
		scope_text = "Single claim"
	elif visit_scope:
		scope_text = "Single visit"
	elif preauth_scope:
		scope_text = "Single preauth"
	else:
		scope_text = "Member (all claims)"
	_member_cell = _cell("Member", _esc(subject_name or cr_id), "#f9fafb", "50%")
	_cr_cell = _cell("CR ID", _esc(cr_id), "#ffffff", "50%")
	_gen_cell = _cell("Generated", _esc(generated) if generated else "&mdash;", "#ffffff", "50%")
	_an_cell = _cell("Analysis", _esc(analysis) if analysis else "&mdash;", "#f9fafb", "50%")
	_ref_cell = _cell("Report Ref", _esc(report_ref) if report_ref else "&mdash;", "#ffffff", "50%")
	_scope_cell = _cell("Scope", scope_text, "#f9fafb", "50%")
	if claim_scope:
		# 4-column grid: subject context up front, run meta below.
		ctx_cells = (
			_cell("Member", _esc(subject_name or cr_id), "#f9fafb", "25%")
			+ _cell("CR ID", _esc(cr_id), "#ffffff", "25%")
			+ _cell("Subject Claim", _esc(subject_claim.strip()), "#f9fafb", "25%")
			+ _cell("Claim Status", status_html, "#ffffff", "25%")
		)
		meta_cells = _gen_cell.replace('width:50%', 'width:25%') + _an_cell.replace('width:50%', 'width:25%') + _ref_cell.replace('width:50%', 'width:25%') + _scope_cell.replace('width:50%', 'width:25%')
		grid_rows = [ctx_cells, meta_cells]
	elif visit_scope:
		# 2-column grid with the subject visit up front (the visit's claims
		# and preauths are the findings below; no single dispatch state).
		grid_rows = [
			_member_cell + _cr_cell,
			_cell("Subject Visit", _esc(subject_visit.strip()), "#f9fafb", "50%") + _gen_cell,
			_an_cell + _ref_cell,
			_scope_cell + _cell("Window", "&mdash;", "#f9fafb", "50%"),
		]
	elif preauth_scope:
		# 2-column grid; the subject preauth's own dispatch state gets the
		# same treatment the claim scope gives the claim.
		grid_rows = [
			_member_cell + _cr_cell,
			_cell("Subject Preauth", _esc(subject_preauth.strip()), "#f9fafb", "50%")
			+ _cell("Preauth Status", status_html if status_text else "&mdash;", "#ffffff", "50%"),
			_gen_cell + _an_cell,
			_ref_cell + _scope_cell,
		]
	else:
		# 2-column grid: subject, then run meta, then ref + scope.
		grid_rows = [
			_member_cell + _cr_cell,
			_gen_cell + _an_cell,
			_ref_cell + _scope_cell,
		]
	grid_html = "".join('<tr>' + row + "</tr>" for row in grid_rows)

	title_row = (
		'<div style="padding:20px 28px 0 28px;">'
		"<table width='100%' cellpadding='0' cellspacing='0'><tr>"
		"<td style='vertical-align:bottom;'>"
		f'<div style="font-size:26px;font-weight:800;color:#111827;line-height:1.15;">{doc_title}</div>'
		f'<div style="font-size:13px;color:#6b7280;margin-top:4px;">{subtitle}</div>'
		"</td>"
		"<td style='vertical-align:middle;text-align:right;'>"
		'<span style="display:inline-block;padding:8px 18px;border-radius:999px;font-size:14px;font-weight:700;'
		f'color:{vfg};background:{vbg};border:1px solid {vfg}44;">{_esc(verdict_text)}</span>'
		"</td>"
		"</tr></table>"
		"</div>"
	)

	stats_row = (
		'<div style="padding:18px 28px 0 28px;">'
		'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:separate;border-spacing:8px 0;">'
		"<tr>"
		+ _stat_cell("Findings", len(findings), None)
		+ _stat_cell("High", counts["high"], "high")
		+ _stat_cell("Medium", counts["medium"], "medium")
		+ "</tr></table>"
		"</div>"
	)
	# Low findings get their own thin line under the stats (keeps the row tidy).
	low_line = ""
	if counts["low"]:
		low_line = (
			'<div style="padding:6px 28px 0 28px;font-size:12px;color:#6b7280;">'
			f"{counts['low']} low-severity finding(s) noted below."
			"</div>"
		)

	# ---- body --------------------------------------------------------------
	summary = str(result.get("executive_summary") or result.get("summary") or "").strip()
	summary_html = (
		'<div style="padding:16px 28px 0 28px;">'
		'<div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#4b5563;">Executive Summary</div>'
		'<div style="border:1px solid #e5e7eb;border-radius:8px;padding:14px 16px;background:#ffffff;'
		'font-size:13px;color:#374151;line-height:1.6;margin-top:8px;white-space:pre-wrap;">'
		+ (_esc(summary) if summary else "No summary produced.")
		+ "</div></div>"
	)

	if findings:
		blocks = "".join(_finding_block(i + 1, f) for i, f in enumerate(findings))
	else:
		blocks = (
			'<div style="border:1px solid #bbf7d0;border-radius:8px;padding:14px 16px;background:#ecfdf3;'
			'font-size:13px;color:#166534;">No discrepancies found.</div>'
		)
	findings_html = (
		'<div style="padding:20px 28px 0 28px;">'
		'<div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#4b5563;margin-bottom:10px;">'
		"Findings</div>"
		+ blocks
		+ "</div>"
	)

	footer_parts = []
	if report_ref:
		footer_parts.append(f"Report {_esc(report_ref)}")
	footer_parts.append("Generated by the Discrepancy Review pipeline (deterministic rules + verified evidence)")
	if sources_text != "&mdash;":
		footer_parts.append(f"Sources: {sources_text}")
	footer = (
		'<div style="padding:24px 28px 16px 28px;border-top:1px solid #e5e7eb;margin-top:8px;">'
		'<div style="font-size:11px;color:#9ca3af;line-height:1.5;">' + " &nbsp;&middot;&nbsp; ".join(footer_parts) + "</div>"
		"</div>"
	)

	return (
		"<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
		"body{margin:0;font-family:Helvetica,Arial,sans-serif;color:#111827;}"
		"ul{margin:4px 0 0 18px;padding:0;}"
		"li{margin-bottom:3px;}"
		"@page{margin:12mm 10mm 14mm 10mm;}"
		"</style></head><body>"
		+ brand_bar
		+ title_row
		+ '<div style="padding:16px 28px 0 28px;">'
		+ '<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;table-layout:fixed;">'
		+ grid_html
		+ "</table></div>"
		+ stats_row
		+ low_line
		+ summary_html
		+ findings_html
		+ footer
		+ "</body></html>"
	)


def build_report_pdf_bytes(
	cr_id: str,
	subject_name: str,
	result: dict,
	generated_at: str | None = None,
	duration_ms=None,
	data_sources: str | None = None,
	report_ref: str | None = None,
	subject_claim: str | None = None,
	subject_visit: str | None = None,
	subject_preauth: str | None = None,
	claim_status: str | None = None,
	claim_status_label: str | None = None,
) -> bytes:
	"""Render the report to PDF bytes (the job's deliverable)."""
	html = render_report_html(
		cr_id,
		subject_name,
		result,
		generated_at=generated_at,
		duration_ms=duration_ms,
		data_sources=data_sources,
		report_ref=report_ref,
		subject_claim=subject_claim,
		subject_visit=subject_visit,
		subject_preauth=subject_preauth,
		claim_status=claim_status,
		claim_status_label=claim_status_label,
	)
	return pdfkit.from_string(html, False)


def attach_report_pdf(report_name: str, pdf_bytes: bytes) -> str:
	"""The run's PDF deliverable: one private File attached to the report
	job. ``content`` must be in the doc at construction time (the platform
	pattern): without it, ``File.is_remote_file`` is True and ``insert()``
	would die in ``validate_file_on_disk``."""
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{report_name}.pdf",
			"is_private": 1,
			"attached_to_doctype": "HISI Discrepancy Report",
			"attached_to_name": report_name,
			"content": pdf_bytes,
		}
	)
	file_doc.insert(ignore_permissions=True)
	return file_doc.name
