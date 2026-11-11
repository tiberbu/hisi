# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""Code-driven benefits detail — the per-intervention registry seam.

The ``HISI Benefit Intervention`` rows (loaded from the SHA benefits
matrices by :mod:`hisi.api.ai.benefits_loader`) are matched against the
codes that actually appear in a claim/preauthorization payload, and only
the matched rows are injected into the prompt as the ``Benefits Detail``
section — row-level tariffs, facility ranges, and document requirements
for exactly the interventions at stake, instead of the whole matrix.

The registry is the source of truth for what a code is: document-code
candidates extracted from the text are only rendered when a row with that
exact code exists, so arbitrary lowercase-hyphen tokens never leak into
the prompt.
"""

import re

import frappe

REGISTRY = "HISI Benefit Intervention"

#: SHA/PMF intervention codes with the matrices' inconsistent spacing
#: (``SHA-19-001``, ``SHA  -19 - 001``, ``SHA-16-7``, ``pmf-12-001`` …).
INTERVENTION_CODE_RE = re.compile(r"\b(SHA|PMF)\s*-\s*(\d{1,2})\s*-\s*(\d{1,3})\b", re.IGNORECASE)

#: Full-code form used for normalization (exact, case/space-insensitive).
_CODE_FULL_RE = re.compile(r"^(SHA|PMF)\s*-\s*(\d{1,2})\s*-\s*(\d{1,3})$", re.IGNORECASE)

#: Lowercase-hyphen document-code-shaped tokens (``xr-chest``, ``us-abdomen``).
DOC_CODE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b")

#: Injection bounds: the section must stay small relative to the data.
MAX_MATCHED = 40
_MAX_CANDIDATES = 200


def normalize_intervention_code(raw) -> str | None:
	"""Normalize a matrix code to ``SHA-19-001`` / ``PMF-12-001`` form.

	Returns ``None`` for anything that is not a full SHA/PMF intervention
	code (partial codes, names, ``N/A``) so junk cells never become rows.
	"""
	match = _CODE_FULL_RE.match(str(raw or "").strip())
	if not match:
		return None
	family, group, seq = match.groups()
	return f"{family.upper()}-{int(group):02d}-{int(seq):03d}"


def extract_benefit_codes(text: str) -> tuple[list[str], list[str]]:
	"""Codes present in a payload: normalized intervention codes + doc tokens.

	The document-code list is NOT validated here — :func:`matched_benefits_lines`
	filters it against the registry, which keeps this function DB-free.
	"""
	codes: set[str] = set()
	for family, group, seq in INTERVENTION_CODE_RE.findall(text or ""):
		codes.add(f"{family.upper()}-{int(group):02d}-{int(seq):03d}")
	docs: set[str] = set(DOC_CODE_RE.findall(text or ""))
	return sorted(codes), sorted(docs)


def matched_benefits_lines(text: str, limit: int = MAX_MATCHED) -> str:
	"""One line per registry row whose code appears in ``text`` ('' if none)."""
	codes, docs = extract_benefit_codes(text)
	if not codes and not docs:
		return ""
	candidates = (codes + docs)[:_MAX_CANDIDATES]
	if not candidates:
		return ""
	rows = frappe.get_all(
		REGISTRY,
		filters={"intervention_code": ["in", candidates]},
		fields=[
			"intervention_code",
			"benefit_code",
			"intervention_name",
			"sub_benefit",
			"complexity",
			"tariff",
			"facility_range",
			"payment_mechanism",
			"frequency",
			"pomf_limit",
			"mandatory_documents",
			"mandatory_investigations",
			"optional_documents",
			"related_codes",
		],
		order_by="benefit_code, intervention_code",
	)
	lines = [_render_row(row) for row in rows]
	lines = [line for line in lines if line]
	if not lines:
		return ""
	if len(lines) > limit:
		lines = lines[:limit] + [f"- ({len(lines) - limit} more matched code(s) elided)"]
	return "\n".join(lines)


def _render_row(row) -> str:
	"""One compact, self-describing line: ``code · name — facts…``."""
	code = str(row.get("intervention_code") or "").strip()
	if not code:
		return ""
	name = str(row.get("intervention_name") or "").strip()
	base = f"- {code} · {name}" if name else f"- {code}"
	parts = []
	sub = str(row.get("sub_benefit") or "").strip()
	if sub:
		parts.append(sub)
	complexity = str(row.get("complexity") or "").strip()
	if complexity:
		parts.append(complexity.lower())
	facility = str(row.get("facility_range") or "").strip()
	if facility:
		parts.append(facility)
	tariff = str(row.get("tariff") or "").strip()
	if tariff:
		if re.fullmatch(r"[\d,\. ]+", tariff):
			parts.append(f"{tariff} KES")
		else:
			parts.append(tariff)
	mechanism = str(row.get("payment_mechanism") or "").strip()
	if mechanism:
		parts.append(mechanism)
	frequency = str(row.get("frequency") or "").strip()
	if frequency:
		parts.append(frequency)
	mandatory_docs = str(row.get("mandatory_documents") or "").strip()
	if mandatory_docs:
		parts.append(f"mandatory documents: {mandatory_docs}")
	investigations = str(row.get("mandatory_investigations") or "").strip()
	if investigations:
		parts.append(f"mandatory investigations: {investigations}")
	optional = str(row.get("optional_documents") or "").strip()
	if optional:
		parts.append(f"optional: {optional}")
	pomf = str(row.get("pomf_limit") or "").strip()
	if pomf:
		parts.append(f"POMF limit: {pomf}")
	related = str(row.get("related_codes") or "").strip()
	if related:
		parts.append(f"related codes: {related}")
	if not parts:
		return base
	line = f"{base} — " + " | ".join(parts)
	return line if len(line) <= 300 else line[:297] + "…"
