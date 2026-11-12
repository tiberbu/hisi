# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""PDF rendering for the ``HISI Discrepancy Report`` job.

The deliverable of a completed run is a PDF: :func:`render_report_html`
builds a self-contained page — a branded masthead (the report brand
wordmark + logo mark on the left, a right-aligned meta block on the
right), a verdict band with severity counts, the executive summary, one
severity-railed block per finding with its evidence, and a footer — and
:func:`build_report_pdf_bytes` renders it with pdfkit/wkhtmltopdf.

The wkhtmltopdf engine is an old WebKit, so the markup is deliberately
plain: block/table layout, floats only for the finding index, lists and
inline-block chips — no flexbox/grid/gradients. Palette mirrors the app
theme (success / warning / destructive tokens).
"""

import base64
import html
from pathlib import Path

import frappe
from hisi import brand

#: (foreground, background, rail) per severity — app theme colors.
_SEVERITY_COLORS = {
	"high": ("#9b1c1c", "#fde8e8", "#9b1c1c"),
	"medium": ("#7a4b00", "#fdf1d8", "#b07d00"),
	"low": ("#47574f", "#eef4f0", "#9db8a9"),
}

#: (foreground, background) per overall level — app theme colors.
_OVERALL_COLORS = {
	"clean": ("#1a7a45", "#e2f4e8"),
	"minor": ("#7a4b00", "#fdf1d8"),
	"major": ("#9b1c1c", "#fde8e8"),
}

_OVERALL_LABELS = {
	"clean": "Clean",
	"minor": "Minor discrepancies",
	"major": "Major discrepancies",
}


def _chip(text: str, fg: str, bg: str) -> str:
	return (
		f'<span style="display:inline-block;border-radius:10px;background:{bg};color:{fg};'
		f"font-size:8pt;font-weight:bold;padding:2px 10px;letter-spacing:.06em;text-transform:uppercase;\">"
		f"{html.escape(text)}</span>"
	)


def _severity_counts(findings: list) -> dict:
	counts = {"high": 0, "medium": 0, "low": 0}
	for finding in findings:
		severity = str(finding.get("severity") or "").strip().lower()
		counts[severity if severity in counts else "medium"] += 1
	return counts


def _stat_cells(findings: list) -> str:
	"""The high/medium/low count strip under the verdict (zero = 0, never a
	placeholder glyph)."""
	counts = _severity_counts(findings)
	parts = ['<table style="width:100%;border-collapse:collapse;margin-top:14px;"><tr>']
	for key in ("high", "medium", "low"):
		fg, bg, _rail = _SEVERITY_COLORS[key]
		parts.append(
			f'<td style="width:33%;padding-right:8px;">'
			f'<div style="border-radius:6px;background:{bg};padding:8px 12px;">'
			f'<div style="font-size:19pt;font-weight:bold;line-height:1.1;color:{fg};">{counts[key]}</div>'
			f'<div style="font-size:7.5pt;font-weight:bold;letter-spacing:.12em;text-transform:uppercase;'
			f"color:{fg};margin-top:2px;\">{key.capitalize()}</div>"
			"</div></td>"
		)
	parts.append("</tr></table>")
	return "".join(parts)


def _finding_block(index: int, finding: dict) -> str:
	severity = str(finding.get("severity") or "medium").strip().lower()
	fg, bg, rail = _SEVERITY_COLORS.get(severity, _SEVERITY_COLORS["medium"])
	code = str(finding.get("tag_code") or "").strip()
	title = str(finding.get("title") or "Untitled finding").strip()
	detail = str(finding.get("detail") or "").strip()
	evidence = [str(e).strip() for e in (finding.get("evidence") or []) if str(e).strip()]

	# The faint index number floats right; the chip + tag code flow left.
	head = (
		f'<span style="float:right;font-size:15px;font-weight:bold;color:#c8d8cf;padding-left:8px;">'
		f"{index:02d}</span>"
		+ _chip(severity.capitalize(), fg, bg)
		+ (
			f' <span style="font-family:monospace;font-size:8.5pt;color:#47574f;">{html.escape(code)}</span>'
			if code
			else ""
		)
	)

	parts = [
		'<div style="border:1px solid #dce8e1;border-radius:8px;overflow:hidden;margin:0 0 12px;">'
		'<table style="width:100%;border-collapse:collapse;">'
		"<tr>"
		f'<td style="width:5px;background:{rail};"></td>'
		'<td style="vertical-align:top;padding:12px 14px 12px 12px;">'
	]
	parts.append(f'<div style="font-size:10pt;">{head}</div>')
	parts.append(
		'<div style="font-weight:bold;font-size:12pt;margin-top:6px;color:#172a22;">'
		f"{html.escape(title)}</div>"
	)
	if detail:
		parts.append(
			'<div style="margin-top:4px;font-size:10.5pt;line-height:1.55;color:#172a22;">'
			f"{html.escape(detail)}</div>"
		)
	if evidence:
		items = "".join(f"<li>{html.escape(item)}</li>" for item in evidence)
		parts.append(
			'<div style="margin-top:8px;border-radius:6px;background:#f7faf8;padding:8px 10px;">'
			'<div style="font-size:7.5pt;font-weight:bold;letter-spacing:.14em;text-transform:uppercase;'
			'color:#47574f;margin-bottom:4px;">Evidence</div>'
			f'<ul style="margin:0;padding-left:14px;font-size:9.5pt;line-height:1.5;color:#47574f;">{items}</ul>'
			"</div>"
		)
	parts.append("</td></tr></table></div>")
	return "".join(parts)


def _report_logo_html() -> str:
	"""The brand mark for the report masthead (the same asset the app UI
	displays). Inlined as a base64 data URI — this wkhtmltopdf build
	refuses file:// and bare-path image references (ProtocolUnknownError) —
	and degrades to no image when the asset is missing, so the report
	renders either way."""
	try:
		path = Path(__file__).resolve().parent / "report_logo.jpeg"
		if path.is_file():
			b64 = base64.b64encode(path.read_bytes()).decode("ascii")
			return (
				'<img src="data:image/jpeg;base64,' + b64 + '" alt="Brand mark" '
				'style="display:block;height:34px;width:auto;margin-top:10px;">'
			)
	except OSError:
		pass
	return ""


def _format_duration(duration_ms) -> str | None:
	"""Human analysis time for the meta block; None when absent/invalid
	(the meta row is dropped rather than showing a placeholder)."""
	try:
		ms = int(duration_ms)
	except (TypeError, ValueError):
		return None
	if ms <= 0:
		return None
	if ms < 1000:
		return f"{ms} ms"
	seconds = ms / 1000
	if seconds < 60:
		return f"{seconds:.1f} s" if seconds < 10 else f"{seconds:.0f} s"
	minutes, sec = divmod(int(seconds), 60)
	return f"{minutes} min {sec} s"


def _section_label(text: str) -> str:
	return (
		f'<div style="font-size:9pt;font-weight:bold;letter-spacing:.16em;color:#0f7a52;'
		f"margin-top:24px;text-transform:uppercase;\">{html.escape(text)}</div>"
		'<div style="border-top:1px solid #dce8e1;margin:4px 0 10px;"></div>'
	)


def render_report_html(
	cr_id: str,
	subject_name: str,
	result: dict,
	generated_at: str | None = None,
	duration_ms=None,
	data_sources: str | None = None,
	report_ref: str | None = None,
) -> str:
	"""The full report page. ``result`` is the normalized payload
	(``summary`` / ``executive_summary`` / ``overall`` / ``findings``);
	``duration_ms``, ``data_sources`` and ``report_ref`` (all optional,
	from the job row) enrich the masthead meta block and the footer."""
	from frappe.utils import now

	overall = str(result.get("overall") or "minor").strip().lower()
	fg, bg = _OVERALL_COLORS.get(overall, _OVERALL_COLORS["minor"])
	executive = str(result.get("executive_summary") or "").strip() or str(result.get("summary") or "").strip()
	findings = [f for f in (result.get("findings") or []) if isinstance(f, dict)]
	stamp = str(generated_at or now()).strip()
	cr_id = str(cr_id or "").strip()
	subject_name = str(subject_name or "").strip()
	report_ref = str(report_ref or "").strip()
	duration = _format_duration(duration_ms)
	sources = str(data_sources or "").strip().replace(",", ", ")

	# Masthead left: the report brand wordmark, then the logo mark.
	mast_left = (
		f'<div style="font-size:10pt;font-weight:bold;letter-spacing:.18em;color:#0f7a52;">'
		f"{html.escape(brand.REPORT_BRAND).upper()}</div>"
		+ _report_logo_html()
	)

	# Masthead right: the document meta block (right-aligned table).
	meta_rows = []
	if report_ref:
		meta_rows.append(
			("Report ref", f'<span style="font-family:monospace;font-size:9.5pt;">{html.escape(report_ref)}</span>')
		)
	meta_rows.append(("Member", f'{html.escape(subject_name or "—")}'))
	if cr_id:
		meta_rows.append(("CR ID", f'<span style="font-family:monospace;font-size:9.5pt;">{html.escape(cr_id)}</span>'))
	meta_rows.append(("Generated", html.escape(stamp)))
	if duration or sources:
		analysis = " · ".join(part for part in (duration, f"sources: {sources}" if sources else "") if part)
		meta_rows.append(("Analysis", html.escape(analysis)))
	meta = "".join(
		'<tr>'
		f'<td style="padding:1.5px 10px 1.5px 0;font-size:7.5pt;font-weight:bold;letter-spacing:.14em;'
		f'text-transform:uppercase;color:#8a9a90;white-space:nowrap;text-align:right;">{key}</td>'
		f'<td style="padding:1.5px 0;font-size:10pt;color:#172a22;text-align:right;">{value}</td>'
		"</tr>"
		for key, value in meta_rows
	)

	if findings:
		verdict_sub = f"{len(findings)} finding{'s' if len(findings) != 1 else ''} flagged"
	else:
		verdict_sub = "No findings flagged"

	title_row = (
		'<table style="width:100%;border-collapse:collapse;"><tr>'
		'<td style="vertical-align:bottom;padding-right:16px;">'
		'<div style="font-size:26pt;font-weight:bold;letter-spacing:-.01em;color:#172a22;line-height:1.15;">'
		"Discrepancy Report</div>"
		'<div style="font-size:9pt;color:#47574f;margin-top:5px;">Claim vs. Shared Health Record '
		"(Visit Level Analysis)</div>"
		"</td>"
		'<td style="vertical-align:bottom;text-align:right;">'
		f'<span style="display:inline-block;border-radius:8px;background:{bg};padding:12px 18px;text-align:right;">'
		f'<span style="font-size:17pt;font-weight:bold;color:{fg};">'
		f"{html.escape(_OVERALL_LABELS.get(overall, _OVERALL_LABELS['minor']))}</span>"
		f'<span style="display:block;font-size:8.5pt;color:{fg};margin-top:3px;">{html.escape(verdict_sub)}</span>'
		"</span></td></tr></table>"
	)

	body = [
		'<div style="height:10px;background:#0f3d28;"></div>',
		'<table style="width:100%;border-collapse:collapse;margin-top:16px;"><tr>',
		f'<td style="vertical-align:top;width:230px;padding-right:16px;">{mast_left}</td>',
		f'<td style="vertical-align:top;"><table style="width:100%;border-collapse:collapse;">{meta}</table></td>',
		"</tr></table>",
		'<div style="border-bottom:2.5px solid #0f7a52;margin:14px 0 18px;"></div>',
		title_row,
		_stat_cells(findings),
		_section_label("Executive Summary"),
		'<div style="font-size:11pt;line-height:1.62;color:#172a22;border-left:3px solid #0f7a52;padding-left:12px;">'
		f"{html.escape(executive) if executive else 'No summary provided.'}</div>",
		_section_label(f"Findings ({len(findings)})"),
	]
	if findings:
		for index, finding in enumerate(findings, 1):
			body.append(_finding_block(index, finding))
	else:
		body.append('<div style="font-size:10pt;color:#47574f;">No discrepancies found in the analyzed data.</div>')

	footer_bits = [f"{html.escape(brand.REPORT_BRAND)} — AI discrepancy analysis"]
	if report_ref:
		footer_bits.append(f"Report {html.escape(report_ref)}")
	footer_bits.append(f"Generated {html.escape(stamp)}")
	body.append(
		'<div style="margin-top:22px;border-top:1px solid #dce8e1;padding-top:8px;font-size:8.5pt;color:#47574f;">'
		" &nbsp;·&nbsp; ".join(footer_bits)
		+ "</div>"
	)

	return (
		"<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>"
		"@page { size: A4; margin: 16mm 14mm; "
		"@bottom-right { content: counter(page); font-size: 8pt; color: #8a9a90; } }"
		"body { font-family: 'DejaVu Sans', sans-serif; font-size: 10pt; color: #172a22; margin: 0; }"
		"</style></head><body>"
		+ "".join(body)
		+ "</body></html>"
	)


def build_report_pdf_bytes(
	cr_id: str,
	subject_name: str,
	result: dict,
	duration_ms=None,
	data_sources: str | None = None,
	report_ref: str | None = None,
) -> bytes:
	"""Render the report to PDF bytes (pdfkit → wkhtmltopdf subprocess).

	The pdfkit import is deferred so a host missing the optional engine
	degrades to a report without a PDF file instead of breaking the whole
	AI module at import time."""
	import pdfkit

	return pdfkit.from_string(
		render_report_html(
			cr_id,
			subject_name,
			result,
			duration_ms=duration_ms,
			data_sources=data_sources,
			report_ref=report_ref,
		),
		False,
	)


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
