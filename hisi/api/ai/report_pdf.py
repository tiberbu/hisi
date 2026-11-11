# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""PDF rendering for the ``HISI Discrepancy Report`` job.

The deliverable of a completed run is a PDF: :func:`render_report_html`
builds a self-contained page (header with the overall verdict, the
executive summary, one block per finding with its evidence, footer) and
:func:`build_report_pdf_bytes` renders it with pdfkit/wkhtmltopdf.

The wkhtmltopdf engine is an old WebKit, so the markup is deliberately
plain: block layout, lists and inline-block chips only — no flexbox/grid.
Palette mirrors the app theme (success / warning / destructive tokens).
"""

import html

import frappe

#: (foreground, background) per severity / overall level — app theme colors.
_SEVERITY_COLORS = {
	"high": ("#9b1c1c", "#fde8e8"),
	"medium": ("#7a4b00", "#fdf1d8"),
	"low": ("#47574f", "#eef4f0"),
}
_OVERALL_COLORS = {
	"clean": ("#1a7a45", "#e2f4e8"),
	"minor": ("#7a4b00", "#fdf1d8"),
	"major": ("#9b1c1c", "#fde8e8"),
}


def _chip(text: str, fg: str, bg: str) -> str:
	return (
		f'<span style="display:inline-block;border-radius:10px;background:{bg};color:{fg};'
		f"font-size:8pt;font-weight:bold;padding:2px 10px;letter-spacing:.03em;\">{html.escape(text)}</span>"
	)


def _finding_block(index: int, finding: dict) -> str:
	severity = str(finding.get("severity") or "medium").strip().lower()
	fg, bg = _SEVERITY_COLORS.get(severity, _SEVERITY_COLORS["medium"])
	code = str(finding.get("tag_code") or "").strip()
	title = str(finding.get("title") or "Untitled finding").strip()
	detail = str(finding.get("detail") or "").strip()
	evidence = [str(e).strip() for e in (finding.get("evidence") or []) if str(e).strip()]

	parts = [
		'<div style="border:1px solid #dce8e1;border-radius:6px;padding:8px 10px;margin:8px 0;">'
	]
	parts.append(
		'<div style="font-size:10pt;">'
		+ _chip(severity.capitalize(), fg, bg)
		+ (f' <span style="font-family:monospace;font-size:8.5pt;color:#47574f;">{html.escape(code)}</span>' if code else "")
		+ "</div>"
	)
	parts.append(
		'<div style="font-weight:bold;font-size:10.5pt;margin-top:4px;">'
		f"{index}. {html.escape(title)}</div>"
	)
	if detail:
		parts.append(
			'<div style="margin-top:4px;font-size:9.5pt;line-height:1.5;color:#172a22;">'
			f"{html.escape(detail)}</div>"
		)
	if evidence:
		items = "".join(f"<li>{html.escape(item)}</li>" for item in evidence)
		parts.append(
			'<div style="margin-top:6px;font-size:8pt;font-weight:bold;color:#47574f;letter-spacing:.08em;">EVIDENCE</div>'
			f'<ul style="margin:2px 0 0 0;padding-left:16px;font-size:9pt;color:#47574f;">{items}</ul>'
		)
	parts.append("</div>")
	return "".join(parts)


def render_report_html(
	cr_id: str,
	subject_name: str,
	result: dict,
	model: str | None = None,
	generated_at: str | None = None,
) -> str:
	"""The full report page. ``result`` is the normalized payload
	(``summary`` / ``executive_summary`` / ``overall`` / ``findings``)."""
	from frappe.utils import now

	overall = str(result.get("overall") or "minor").strip().lower()
	fg, bg = _OVERALL_COLORS.get(overall, _OVERALL_COLORS["minor"])
	executive = str(result.get("executive_summary") or "").strip() or str(result.get("summary") or "").strip()
	findings = [f for f in (result.get("findings") or []) if isinstance(f, dict)]
	stamp = str(generated_at or now()).strip()

	body = [
		'<div style="border-bottom:2px solid #0f7a52;padding-bottom:8px;">',
		'<div style="font-size:8pt;font-weight:bold;color:#0f7a52;letter-spacing:.12em;">HISI · CLAIM ↔ SHR AUDIT</div>',
		'<div style="font-size:18pt;font-weight:bold;margin-top:2px;">Discrepancy Report</div>',
		'<div style="font-size:9pt;color:#47574f;margin-top:6px;">'
		+f"Member: <b style=\"color:#172a22;\">{html.escape(str(subject_name or '—'))}</b> ({html.escape(str(cr_id or 'unknown member'))})"
		+ f" &nbsp;·&nbsp; Generated {html.escape(stamp)}"
		+ (f" &nbsp;·&nbsp; Model {html.escape(str(model))}" if model else "")
		+ " &nbsp;·&nbsp; "
		+ _chip(overall.capitalize(), fg, bg)
		+ "</div>",
		"</div>",
	]

	body.append('<div style="font-size:11pt;font-weight:bold;color:#0f7a52;margin-top:16px;">Executive Summary</div>')
	body.append(
		'<div style="font-size:10pt;line-height:1.55;margin-top:4px;color:#172a22;">'
		f"{html.escape(executive) if executive else 'No summary provided.'}</div>"
	)

	body.append(
		'<div style="font-size:11pt;font-weight:bold;color:#0f7a52;margin-top:16px;">'
		f"Findings ({len(findings)})</div>"
	)
	if findings:
		for index, finding in enumerate(findings, 1):
			body.append(_finding_block(index, finding))
	else:
		body.append('<div style="font-size:10pt;color:#47574f;margin-top:4px;">No discrepancies found in the analyzed data.</div>')

	body.append(
		'<div style="margin-top:20px;border-top:1px solid #dce8e1;padding-top:6px;font-size:8pt;color:#47574f;">'
		f"Generated {html.escape(stamp)}"
		+ (f" · model {html.escape(str(model))}" if model else "")
		+ " · Tiberbu HISI — AI discrepancy analysis"
		+ "</div>"
	)

	return (
		"<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>"
		"@page { size: A4; margin: 16mm 14mm; }"
		"body { font-family: 'DejaVu Sans', sans-serif; font-size: 10pt; color: #172a22; margin: 0; }"
		"</style></head><body>"
		+ "".join(body)
		+ "</body></html>"
	)


def build_report_pdf_bytes(
	cr_id: str,
	subject_name: str,
	result: dict,
	model: str | None = None,
) -> bytes:
	"""Render the report to PDF bytes (pdfkit → wkhtmltopdf subprocess).

	The pdfkit import is deferred so a host missing the optional engine
	degrades to a report without a PDF file instead of breaking the whole
	AI module at import time."""
	import pdfkit

	return pdfkit.from_string(render_report_html(cr_id, subject_name, result, model), False)


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
