# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""Prompt assembly + reply parsing for the AI discrepancy endpoints.

The report prompt is EXACTLY four concatenated sections (the VidVal
pattern): system instructions, the compact data context, one line per
declared tag, and the fixed ``OUTPUT_CONTRACT`` appended VERBATIM
(byte-for-byte). The reply parser is tolerant (markdown fences / leading
prose → outermost balanced ``{…}``), and ``normalize_findings`` shapes the
parsed object deterministically (dedupe, severity coercion, overall
recomputation) so the stored result never depends on model drift.
"""

import json
import re

from frappe.utils import now

DEFAULT_SYSTEM_PROMPT = (
	"You are a health-insurance claims auditor. You are given a member's data "
	"(consents, claims, and preauthorizations) and a "
	"list of discrepancy checks. Compare the data strictly against the checks "
	"and report each discrepancy you can support with the data.\n"
	"The member's contribution ledger is NOT part of the analysis: contribution "
	"decisions are made by the payer, so never flag, score or comment on "
	"contribution payments, gaps or amounts in the summary.\n"
	"When a visits section is present, each visit bucket holds the visit's "
	"consent state, the Shared Health Record (SHR) resources submitted for "
	"that visit, and the claim/preauthorization records linked to it. Compare "
	"the claim details against the SHR details visit by visit — patient "
	"identity, facility, service dates, diagnoses, services and amounts — and "
	"flag a visit whose claims are not covered by an approved consent, or a "
	"consented visit with no linked claims, when the data supports it."
)

#: Built-in benefits knowledge — condensed from the SHA Benefits Matrix
#: (05.06.25) and the Reviewed POMSF Benefits Matrix (19.11.25): the
#: two-payer model, job-group caps, key tariffs, preauthorization
#: requirements, global periods, and claim-composition rules. Injected as
#: the ``Benefits Reference:`` section of the report prompt (and the chat
#: system message) whenever the configured ``benefits_reference`` setting is
#: empty. Audit criteria only — accuracy rule (3) keeps the model from
#: reporting it as data about the member.
DEFAULT_BENEFITS_REFERENCE = """Kenya SHA benefits reference for auditing preauthorizations and claims (benefit packages effective 01/10/2024). Use it strictly as audit criteria — tariff, cap, and coverage benchmarks — never as a description of the member's data.

FACILITY LEVELS: the facility scale is 2, 3, 3A, 3B, 3C, 4, 4A, 4B, 4C, 5, 6, 6A, 6B. A matrix range such as "Level 4 to Level 6" covers every level between the endpoints (4, 4A, 4B, 4C, 5, 6, 6A, 6B). A claim/preauth at a facility level outside the intervention's permitted range is a discrepancy.

TWO-PAYER MODEL: first payer SHA (ECCIF/SHIF) pays the gazetted tariff; second payer POMF pays amounts over the SHA tariff within the member's allocation.

JOB-GROUP CAPS (POMF, KES, per individual): inpatient B5/C1/C2 1,000,000; C3 1,300,000; C4 1,500,000; C5 1,800,000; D1 2,000,000; D2 2,200,000; D3 2,500,000; D4 2,800,000; D5 3,000,000. Outpatient 150,000 (B5-C2) up to 450,000 (D5). Dental 45,000 (per household). Optical 60,000. Maternity 120,000 (B5-C5), 200,000 (D1-D2), 250,000 (D3-D4), 300,000 (D5). Overseas treatment 2,200,000 (SHA pays up to 500,000 first). Last-expense 300,000.

KEY TARIFFS (KES): inpatient per diem by facility level — L3 2,240; L4 3,360; L5 3,920; L6 4,480 (180 days per household, shared across inpatient / mental wellness / surgery / maternity / palliative). Critical care L4-6 28,000/day, max 12 days per admission episode, after which normal per diem applies. Maternity case-based: vaginal delivery 10,000; caesarean section 30,000 (female only; over 55 years goes to clinical review). Surgical services fixed fee per procedure, tiered by complexity (minor / major / specialized). Medical imaging fixed fee (e.g. CT head with contrast 6,900; CT angiography 8,000; PET scan 53,500). Optical 950 (PHC) / up to 1,500 (POMSF), under 18, once every two years.

EMERGENCY CARE (A&E protocols, L3-6): protocol-based, each with a maximum claimable amount — cardiac arrest 102,915; stroke 106,233; acute coronary syndrome 107,633; major trauma 93,042; shock 74,898; seizures 21,106; altered consciousness 20,656; respiratory distress 14,315; anti-rabies 2,600; anti-snake 6,600. All emergency interventions require the doctor's prior OTP authorization; a claim above its protocol maximum is a discrepancy.

RENAL (SHA-16, L3B-6): hemodialysis 10,650/session (max 2/week, 104/year); hemodiafiltration 10,650; dialysis for acute kidney injury 10,650; permanent dialysis catheter 50,000; peritoneal dialysis 85,200; CAPD catheter insertion 33,600; vascular access (catheters/AV fistula/graft) 78,400; pre-transplant recipient evaluation up to 150,000 (L5-6); renal transplant recipient 700,000 (L6 only); simple nephrectomy 168,000 (L6); post-renal replacement therapy 16,667.

HEMATO-ONCOLOGY (SHA-06, L3-6, FFS): SHIF individual limit 400,000; chemotherapy treatment transitions to ECCIF after SHIF exhaustion, +150,000. Diagnosis & staging: histology 2,500 / 3,500 / 5,000 (small/medium/large specimen); immunohistochemistry 3,500 per marker; cytology 1,500; flow cytometry 2,500 per marker; bone marrow aspiration cytology 2,200; trephine biopsy 3,500; PCR 8,000 per marker; CT for planning 6,900; MRI 11,000; PET scan 53,500; PSMA PET 64,200; radionuclide scan 11,000; bone scan 15,000. Treatment: chemotherapy administration 5,500; chemotherapy medicines up to 400,000; radiotherapy 3,600 per fraction; brachytherapy up to 40,000; SBRT up to 70,000; red cell exchange 70,000 (max 3); apheresis platelet transfusion 20,000 (max 3); chemo port insertion 15,000; routine labs FBC 500, urea/electrolytes/creatinine 1,000, liver function 1,000 (max 4 each).

ESSENTIAL NCD LABS (SHA-18, L4-6, FFS, frequency-limited 1-2x/year per test): HbA1c 1,000; urine microalbumin 700; creatinine 300; cholesterol 300; proBNP 2,000; urinalysis 250; full blood count 500; hemoglobin electrophoresis 2,000; total bilirubin 600; ALT 300; alpha-1 antitrypsin 700.

POMSF-ONLY CATEGORIES (SHA tariff N/A — second payer only): oral health SHA-11 (L2-6, outpatient, 45,000 per-household cap) — ~49 fixed fees 500 to 30,000 (extractions 1,000-7,500; fillings 3,000-4,000; root canal 8,000-12,000; crowns/inlays 10,000-20,000; braces 30,000; retainers 15,000). Rehabilitative SHA-15 (L2-6): physical, occupational, speech-language, cognitive, psychotherapy. Screening SHA-17: male (SHA-17-007) / female (SHA-17-008) wellness-check packages. Assistive devices SHA-02 (L4-6): hearing aids, crutches, club-foot brace, walking frames, therapeutic wear, wheelchair, surgical belt — FFR within the inpatient cap.

PREAUTHORIZATION REQUIREMENTS: every surgical procedure requires prior authorization (the requesting doctor receives a USSD OTP code). New renal dialysis cases require manual preauth at the 3rd session. CT/MRI imaging is limited to six board-approved conditions (infective; haemato-oncology; neurological degenerative; specific obstetric; cardiac/CVA-related; trauma) and must be requested by a credentialed specialist. The benefits balance is checked immediately after a preauth is approved.

GLOBAL PERIODS & TIMING: surgical global periods — minor 1 night; major 3 nights; specialized 7 nights (extra nights at the gazetted rebate rate). During the surgical global period the inpatient / critical care / mental wellness / palliative per diem must NOT run concurrently; procedure or discharge dates beyond the global period go to clinical review. Maternity complication per diem applies only after 48 hours (normal delivery) / 72 hours (caesarean). A same-day repeat surgery or an unplanned return to theatre for postoperative complications goes to clinical review. A secondary simultaneous procedure reimburses at 50% of its gazetted tariff.

COMBINATION RULES: one visit may carry several benefit packages, each submitted as a single claim sharing the visit ID; per-diem days accumulate to a maximum of 180 across all household members. No-combo packages (must not be combined with other intervention codes): optical SHA-05, haemato-oncology SHA-06, medical imaging SHA-09, mental wellness SHA-10, outpatient SHA-12, essential NCD labs SHA-18. Critical care SHA-03 may combine with inpatient SHA-07, haemato-oncology SHA-06, renal SHA-16, imaging SHA-09, palliative SHA-13, maternity SHA-08, and surgery SHA-19.

POLICY WINDOW: formal sector — from the date of registration (w.e.f. 2024-10-01); informal sector — 365 days from receipt of the annual premium. Services outside the active coverage window are discrepancies.

SURGICAL SERVICE CODES (SHA-19): ~720 interventions across cardiology, cardiothoracic and vascular, ENT, general, interventional radiology, maxillofacial, neurosurgery, obstetrics and gynaecological, ophthalmic, orthopaedic, paediatric, plastic, urological, and daycare procedures. EVERY SHA-19 procedure requires prior authorization (USSD OTP) plus a Preauthorization form and Clinical Notes (a Case Summary is also mandatory for a subset), together with procedure-specific mandatory investigations (e.g. cardiology: electrocardiogram and echocardiogram; vascular work: CT/CTA/MRA; abdominal-pelvic: ultrasound). A claim or preauth referencing an SHA-19 intervention that lacks a preauthorization record or the mandatory documents/investigations is a discrepancy.

DOCUMENT/IMAGING CODE CONVENTION (used throughout claims and preauthorization data): lowercase, words hyphen-joined, only a-z, 0-9 and '-'. Imaging codes are modality-bodypart-qualifier. Prefixes: xr (plain X-ray, e.g. xr-chest), ct (CT), cta/ctv/ctu (CT angiography/venography/urography), hrct (high-resolution CT), cbct (cone-beam CT), mri, mra/mrv/mrcp, mr-arthrogram, us (ultrasound incl. duplexer), eus (endoscopic ultrasound), ubm, echo (echocardiogram), mg (mammography), fl (fluoroscopy/contrast), xa (catheter angiography), nm (nuclear medicine), pet-ct, oct, and opg/hsg/mcug/rug. Non-imaging documents use a lowercase hyphen-joined name (e.g. preauthorization-form, clinical-notes, case-summary)."""


def effective_benefits_reference(benefits_reference: str | None) -> str:
	"""The configured benefits reference, or the built-in default (trimmed).

	Mirrors the system-prompt fallback: an empty configured value means
	"use the built-in knowledge", never "no knowledge"."""
	return (benefits_reference or "").strip() or DEFAULT_BENEFITS_REFERENCE

#: The fixed Output Contract — the canonical fixed text, appended to the
#: prompt VERBATIM (byte-for-byte). It demands JSON-only output (no markdown,
#: no commentary) in exactly the discrepancy-report shape and pins the
#: accuracy rules. The tolerant parser holds the reply to this contract.
OUTPUT_CONTRACT = (
	"Compare the data strictly against the checks above. Respond with ONLY a JSON "
	"object, no markdown, no commentary, in exactly this shape:\n"
	'{"summary": string, "executive_summary": string, "overall": "clean|minor|major", '
	'"findings": [{"tag_code": string, "severity": "low|medium|high", '
	'"title": string, "detail": string, "evidence": [string]}]}\n'
	"`summary` is one or two sentences stating the overall result. "
	"`executive_summary` is a management-level overview of three to six sentences: "
	"what was audited (data sources and period), the most significant discrepancies "
	"and their likely financial or compliance impact, and what should happen next — "
	"written for a reviewer who will not read the line items.\n"
	"Include one finding for every check you can evaluate against the data; omit "
	"checks the data does not cover.\n"
	"ACCURACY RULES (these override every other instruction): (1) Base every "
	"finding ONLY on the data above — never invent records, dates, amounts, or "
	"identifiers. (2) Transcribe amounts, dates, reference numbers and status "
	"labels EXACTLY as given in the data. (3) The system instructions and check "
	"descriptions above are criteria only — they are NOT a description of what "
	"the data contains; do not report them as observations. (4) If a check "
	"cannot be evaluated because the relevant data is missing or errored, omit "
	"the finding for that check. (5) Every `evidence` string must cite the "
	"specific record it relies on — its reference number, date, or amount — so "
	"the claim can be re-verified against the raw data."
)

#: The one corrective-retry message (the raw reply is echoed back as an
#: ``assistant`` turn, this as a ``user`` turn).
CORRECTIVE_MESSAGE = "Your last reply was not valid JSON. Reply with the JSON object only."

OVERALL_LEVELS = ("clean", "minor", "major")
SEVERITIES = ("low", "medium", "high")

_CODE_FENCE_RE = re.compile(r"```[A-Za-z0-9_]*[ \t]*\r?\n?(.*?)```", re.DOTALL)


def _balanced_object_spans(text: str):
	"""Yield ``(start, end)`` spans of balanced ``{...}`` objects, in order.

	String-aware: braces inside JSON strings (and ``\\`` escapes) do not
	affect the depth count. An opening brace whose span never balances (e.g.
	stray prose braces) is skipped and scanning resumes just past it.
	"""
	n = len(text)
	i = 0
	while i < n:
		if text[i] != "{":
			i += 1
			continue
		depth = 0
		in_string = False
		escaped = False
		for j in range(i, n):
			c = text[j]
			if in_string:
				if escaped:
					escaped = False
				elif c == "\\":
					escaped = True
				elif c == '"':
					in_string = False
			elif c == '"':
				in_string = True
			elif c == "{":
				depth += 1
			elif c == "}":
				depth -= 1
				if depth == 0:
					yield i, j + 1
					break
		i += 1


def extract_json_object(raw_text) -> dict | None:
	"""Locate and parse the model's JSON object in a raw reply.

	Strips markdown fences / leading prose by scanning for the outermost
	balanced ``{...}`` span: the fence interior (when a fence is present) is
	tried first, then the full text; the first balanced span that parses as a
	JSON *object* wins. Returns ``None`` when no such object exists — the
	caller performs the corrective retry.
	"""
	if not raw_text or not isinstance(raw_text, str):
		return None
	text = raw_text.strip()
	candidates = [text]
	match = _CODE_FENCE_RE.search(text)
	if match:
		candidates.insert(0, match.group(1))
	for candidate in candidates:
		for start, end in _balanced_object_spans(candidate):
			try:
				obj = json.loads(candidate[start:end])
			except (json.JSONDecodeError, ValueError):
				continue
			if isinstance(obj, dict):
				return obj
	return None


def parse_llm_json(raw_text) -> dict | None:
	"""Tolerant parse of a raw reply against the OUTPUT_CONTRACT shape.

	Returns ``{"summary", "executive_summary", "overall", "findings"}`` (findings
	as the raw list — :func:`normalize_findings` shapes it) or ``None`` when the
	reply is not a JSON object carrying a ``findings`` list (the caller retries).
	"""
	obj = extract_json_object(raw_text)
	if not isinstance(obj, dict):
		return None
	if "findings" not in obj or not isinstance(obj.get("findings"), list):
		return None
	overall = str(obj.get("overall") or "").strip().lower()
	if overall not in OVERALL_LEVELS:
		overall = "minor"
	return {
		"summary": str(obj.get("summary") or "").strip(),
		"executive_summary": str(obj.get("executive_summary") or "").strip(),
		"overall": overall,
		"findings": obj["findings"],
	}


def normalize_findings(parsed: dict) -> dict:
	"""Shape a parsed reply deterministically.

	Findings are de-duplicated, severity-coerced (unknown/missing →
	``medium``), evidence-coerced to a list of non-empty strings, and
	titled (a missing title falls back to the tag code). ``overall`` is
	RECOMPUTED from the findings (no findings → ``clean``; any medium/high
	finding → ``major``; else ``minor``) — the model's own value is a hint
	only, mirroring the verdict-recomputation rule.
	"""
	seen = set()
	findings = []
	for item in parsed.get("findings") or []:
		if not isinstance(item, dict):
			continue
		tag_code = str(item.get("tag_code") or "").strip()
		severity = str(item.get("severity") or "medium").strip().lower()
		if severity not in SEVERITIES:
			severity = "medium"
		title = str(item.get("title") or "").strip() or tag_code or "Untitled finding"
		key = (tag_code, title, str(item.get("detail") or "").strip())
		if key in seen:
			continue
		seen.add(key)
		evidence = item.get("evidence")
		if isinstance(evidence, str):
			evidence = [evidence]
		if not isinstance(evidence, list):
			evidence = []
		findings.append(
			{
				"tag_code": tag_code,
				"severity": severity,
				"title": title,
				"detail": str(item.get("detail") or "").strip(),
				"evidence": [str(e) for e in evidence if str(e).strip()],
			}
		)
	if not findings:
		overall = "clean"
	elif any(f["severity"] in ("medium", "high") for f in findings):
		overall = "major"
	else:
		overall = "minor"
	return {
		"summary": str(parsed.get("summary") or "").strip(),
		"executive_summary": str(parsed.get("executive_summary") or "").strip(),
		"overall": overall,
		"findings": findings,
	}


def render_report_markdown(
	cr_id: str,
	subject_name: str,
	result: dict,
	tags_by_code: dict | None = None,
	coverage: dict | None = None,
	contribution_ledger: list | None = None,
	narrative_state: str | None = None,
) -> str:
	"""Render the normalized result as the stored markdown report.

	``coverage`` (per-lane status + counts) and ``contribution_ledger``
	(sorted entries) mirror the PDF: the Coverage section states what the
	review was based on; the ledger groups the contribution data in one
	context-only section (it never drives findings). ``narrative_state``
	says in the report itself why the AI narrative was skipped/rejected.
	"""
	lines = ["# Discrepancy Report — " + (str(cr_id or "unknown member"))]
	if subject_name:
		lines.append("")
		lines.append(f"Member: {subject_name}")
	lines.append("")
	lines.append(f"Overall: **{result.get('overall')}** · Findings: {len(result.get('findings') or [])}")
	lines.append("")
	if coverage and coverage.get("lanes"):
		lines += ["## Coverage — what this review evaluated", ""]
		if coverage.get("scope"):
			lines += [f"Scope: {coverage['scope']}", ""]
		lines += ["| Lane | Status | Records |", "| --- | --- | ---: |"]
		for lane in coverage["lanes"]:
			count = lane.get("count")
			lines.append(
				f"| {lane.get('label') or lane.get('key')} | {str(lane.get('status') or 'unknown').capitalize()}"
				f" | {count if count is not None else '—'} |"
			)
			if str(lane.get("status")) == "error" and str(lane.get("detail") or "").strip():
				lines.append(f"| | — | {lane['detail']} |")
			lines.append("")
		if any(l.get("status") in ("empty", "error") for l in coverage["lanes"]):
			lines += ["Lanes marked Empty or Errored were not part of the evidence; findings depend only on the available lanes.", ""]
	if result.get("summary"):
		lines += ["## Summary", ""]
		if narrative_state:
			lines += [f"_{narrative_state}_", ""]
		lines += [str(result["summary"]), ""]
	lines += ["## Findings", ""]
	findings = result.get("findings") or []
	if findings:
		for index, finding in enumerate(findings, 1):
			code = f" ({finding['tag_code']})" if finding.get("tag_code") else ""
			lines.append(f"### {index}. [{finding['severity']}] {finding['title']}{code}")
			if finding.get("detail"):
				lines += ["", finding["detail"], ""]
			if finding.get("evidence"):
				lines.append("**Evidence**")
				lines += [f"- {e}" for e in finding["evidence"]]
				lines.append("")
	else:
		lines += ["No discrepancies found in the analyzed data.", ""]
	if contribution_ledger:
		lines += [
			"## Contribution ledger",
			"",
			"_Context only — contribution decisions are made by the payer and are not part of this analysis._",
			"",
			"| Period | Amount | Reference |",
			"| --- | ---: | --- |",
		]
		for entry in contribution_ledger:
			amount = entry.get("amount")
			try:
				amount_text = f"{float(amount):,.2f}".rstrip("0").rstrip(".") if float(amount) else "—"
			except (TypeError, ValueError):
				amount_text = "—"
			ref = str(entry.get("payment_reference") or entry.get("invoice_number") or "").strip() or "—"
			lines.append(f"| {str(entry.get('period') or '').strip() or '—'} | {amount_text} | {ref} |")
		lines.append("")
	lines.append(f"Generated {now()}")
	return "\n".join(lines).rstrip() + "\n"


def _tag_line(tag: dict) -> str:
	"""One Checks line: ``- <code>: <label> — <instruction> (severity: <s>)``."""
	code = str(tag.get("tag_code") or "").strip()
	label = str(tag.get("label") or "").strip()
	instruction = str(tag.get("instruction") or "").strip()
	severity = str(tag.get("severity") or "medium").strip().lower()
	line = f"- {code}: {label}" if label else f"- {code}"
	if instruction:
		line += f" — {instruction}"
	if severity in SEVERITIES:
		line += f" (severity: {severity})"
	return line


def build_report_prompt(
	system_prompt: str | None,
	context: dict,
	tags: list[dict],
	benefits_reference: str | None = None,
	benefits_detail: str | None = None,
) -> str:
	"""Assemble the report prompt (fixed sections, one user turn).

	``{system}`` + (optionally) ``\\n\\nBenefits Reference:\\n{benefits}`` +
	(optionally) ``\\n\\nBenefits Detail:\\n{detail}`` (the registry rows
	matched to codes present in the data) +
	``\\n\\nData:\\n{context JSON}`` + ``\\n\\nChecks:\\n{tag lines}`` +
	``\\n\\n{OUTPUT_CONTRACT}`` — the contract is appended verbatim,
	byte-for-byte. An empty ``system_prompt`` falls back to the documented
	default and an empty ``benefits_reference`` falls back to the built-in
	SHA benefits knowledge (``effective_benefits_reference``), so the worker
	is never blocked by an unconfigured single.
	"""
	system = (system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT
	benefits = effective_benefits_reference(benefits_reference)
	benefits_section = f"\n\nBenefits Reference:\n{benefits}" if benefits else ""
	detail = (benefits_detail or "").strip()
	detail_section = f"\n\nBenefits Detail (registry rows for codes present in the data):\n{detail}" if detail else ""
	lines = "\n".join(_tag_line(tag) for tag in tags) if tags else "- (none declared)"
	data = json.dumps(context, indent=2, ensure_ascii=False, default=str)
	return f"{system}{benefits_section}{detail_section}\n\nData:\n{data}\n\nChecks:\n{lines}\n\n{OUTPUT_CONTRACT}"


#: Per-visit caps for the claim<->SHR comparison section: a submitted bundle
#: carries ~5-6 resources and a visit rarely links more than a handful of
#: claims, so these stay well under ``max_records`` while bounding the
#: prompt size.
_VISIT_MAX_RESOURCES = 12
_VISIT_MAX_LINKED_CLAIMS = 10


def build_data_context(cr_id: str, sources: list[str], results: dict, errors: dict, max_records: int) -> dict:
	"""The compact data section of the prompt: one trimmed entry per source.

	Each step result is included with its record-like lists capped at
	``max_records`` entries; per-source fetch errors are carried in
	``errors`` so the model can see which checks it must omit (accuracy rule
	4) instead of guessing. When the worker attached the per-visit join
	(``results["visits"]``), a ``visits`` section is added: one bucket per
	visit with the consent state, the Shared Health Record resources
	submitted for the visit (compact FHIR views), and the claim/preauth
	records linked to it — the per-visit claim<->SHR comparison data.

	The contribution lane is excluded: contributions are context only (the
	payer makes contribution decisions) — shown in a dedicated report section
	rather than fed to the model to comment on. The raw ``records`` lane is
	also excluded from the data section: the ``visits`` join already carries
	the per-visit Shared Health Record resources (briefed) + linked claims, so
	the full lane would only bloat the prompt (fetch errors still flow through
	``errors`` so accuracy rule 4 keeps working).
	"""
	data = {}
	for step in sources:
		if step in results and step not in ("contributions", "records"):
			data[step] = _trim_step(results[step], max_records)
	context = {
		"cr_id": str(cr_id or ""),
		"sources": list(sources),
		"data": data,
		"errors": {key: str(value) for key, value in (errors or {}).items()},
	}
	visits = results.get("visits")
	if isinstance(visits, list):
		visit_context = [_visit_context(visit) for visit in visits[:max_records] if isinstance(visit, dict)]
		if visit_context:
			context["visits"] = visit_context
	return context


def _visit_context(visit: dict) -> dict:
	"""One per-visit comparison bucket for the prompt."""
	out = {
		"visit_id": str(visit.get("visit_id") or ""),
		"consented": visit.get("consented"),
	}
	for key in ("facility_id", "consent_status", "consent_creation"):
		if visit.get(key) not in (None, ""):
			out[key] = visit.get(key)
	shr = [resource for resource in (visit.get("shr_resources") or []) if isinstance(resource, dict)]
	out["shr_resources"] = [_shr_resource_brief(resource) for resource in shr[:_VISIT_MAX_RESOURCES]]
	claims = [claim for claim in (visit.get("linked_claims") or []) if isinstance(claim, dict)]
	out["linked_claims"] = claims[:_VISIT_MAX_LINKED_CLAIMS]
	return out


#: The claim<->SHR comparison fields per FHIR resource type — the
#: identifiers, dates, parties and services the checks actually compare
#: (meta/extension noise dropped).
_SHR_BRIEF_FIELDS = {
	"Encounter": ("id", "status", "class", "period", "episodeOfCare", "identifier", "serviceProvider"),
	"Claim": ("id", "status", "type", "use", "billablePeriod", "created", "patient", "provider", "diagnosis", "item", "total"),
	"Patient": ("id", "identifier", "name", "gender", "birthDate"),
	"Coverage": ("id", "identifier", "status", "beneficiary"),
	"Organization": ("id", "name", "identifier"),
}


def _shr_resource_brief(resource: dict) -> dict:
	"""A compact view of one Shared Health Record FHIR resource: the
	``resourceType`` plus the comparison fields for that type (unknown types
	carry their ``id`` only)."""
	rtype = str(resource.get("resourceType") or "")
	brief = {"resourceType": rtype}
	for key in _SHR_BRIEF_FIELDS.get(rtype, ("id",)):
		value = resource.get(key)
		if value not in (None, "", [], {}):
			brief[key] = value
	return brief


def _trim_step(result, max_records: int):
	"""Cap the record-like lists of one step result (deterministic order)."""
	if isinstance(result, list):
		return result[:max_records]
	if not isinstance(result, dict):
		return result
	out = dict(result)
	for key in ("records", "contributions", "schemes", "dependants", "other_identifications"):
		if isinstance(out.get(key), list):
			out[key] = out[key][:max_records]
	if isinstance(out.get("by_visit"), dict):
		merged = []
		for visit in sorted(out["by_visit"]):
			merged.extend(out["by_visit"].get(visit) or [])
		out.pop("by_visit")
		out["records"] = merged[:max_records]
	if isinstance(out.get("by_resource"), dict):
		merged = []
		for rtype in sorted(out["by_resource"]):
			merged.extend(out["by_resource"].get(rtype) or [])
		out.pop("by_resource")
		out["records"] = (out.get("records") or [])[:max_records] + merged[:max_records]
	return out
