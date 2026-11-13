# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""The PII redaction layer — the boundary between fetched data and the LLM.

Two contexts exist per run: the FULL context (rules, evidence verification,
PDF) and the REDACTED context (anything sent to any model). This module
builds the redacted copy. The default policy masks: phone numbers, full
identifiers (CR IDs / national IDs), dates of birth beyond the year, names
(except "the member"), and email addresses. The policy is a JSON dict on
``HISI Report Settings`` (``redaction_policy``) with boolean toggles:

    {"phone": true, "cr_id": true, "id_numbers": true, "dob": true,
     "names": true, "email": true}

Rules and the PDF read the full context, so the deliverable stays complete
while models see minimal PII.
"""

import copy
import re

#: Default policy — everything masked from day one (the first non-negotiable
#: gate of the v2 design).
DEFAULT_POLICY = {
	"phone": True,
	"cr_id": True,
	"id_numbers": True,
	"dob": True,
	"names": True,
	"email": True,
}

_PHONE_RE = re.compile(r"(?<![\d+])(\+?\d{1})(\d{3})(\d{2,})(?!\d)")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_DOB_KEYS = ("date_of_birth", "birthDate")
_NAME_KEYS = (
	"name",
	"first_name",
	"middle_name",
	"last_name",
	"full_name",
	"client_name",
	"employer_name",
	"verified_by",
	"practitioner_name",
)
_ID_KEYS = (
	"cr_id",
	"identification_number",
	"id",
	"id_serial",
	"disability_certificate_no",
)


def _mask_phone(value: str) -> str:
	return _PHONE_RE.sub(r"\1•••\3", value)


def _mask_email(value: str) -> str:
	return _EMAIL_RE.sub(r"•••@\2", value)


def _mask_id(value: str) -> str:
	"""Keep the first two and last two characters (``CR••••AB12`` style)."""
	text = value.strip()
	if len(text) <= 4:
		return "•" * len(text)
	if len(text) <= 8:
		return text[:2] + "•" * (len(text) - 4) + text[-2:]
	return text[:2] + "•" * 4 + text[-2:]


def _mask_dob(value: str) -> str:
	text = str(value or "").strip()
	if len(text) >= 4 and text[:4].isdigit():
		return text[:4]
	return ""


def _redact_value(value, policy: dict):
	"""Redact one scalar by policy. Non-strings pass through untouched."""
	if not isinstance(value, str) or not value.strip():
		return value
	out = value
	if policy.get("phone"):
		out = _mask_phone(out)
	if policy.get("email"):
		out = _mask_email(out)
	return out


def redact_context_for_llm(context: dict, policy: dict | None = None) -> dict:
	"""Build the redacted copy of a data context for LLM consumption.

	``context`` is the ``build_data_context`` shape (``{cr_id, sources, data,
	errors}`` plus optional ``visits``). The returned dict has the same
	shape; PII fields are masked per ``policy`` (default: all on).
	"""
	policy = {**DEFAULT_POLICY, **(policy or {})}
	out = copy.deepcopy(context)

	cr_id = str(out.get("cr_id") or "")
	if policy.get("cr_id") and cr_id:
		out["cr_id"] = _mask_id(cr_id)

	data = out.get("data")
	if not isinstance(data, dict):
		return out
	for step, result in data.items():
		if step == "patient" and isinstance(result, dict):
			_redact_member(result, policy)
		elif step == "contributions" and isinstance(result, dict):
			for entry in result.get("contributions") or []:
				if isinstance(entry, dict) and entry.get("employer"):
					entry["employer"] = "•••" if policy.get("names") else entry["employer"]
		elif isinstance(result, dict):
			records = result.get("records")
			if isinstance(records, list):
				for record in records:
					if isinstance(record, dict):
						_redact_record(record, policy)
		elif isinstance(result, list):
			for record in result:
				if isinstance(record, dict):
					_redact_record(record, policy)

	visits = out.get("visits")
	if isinstance(visits, list):
		for visit in visits:
			if not isinstance(visit, dict):
				continue
			for claim in visit.get("linked_claims") or []:
				if isinstance(claim, dict):
					_redact_record(claim, policy)
			for resource in visit.get("shr_resources") or []:
				if isinstance(resource, dict):
					_redact_fhir_brief(resource, policy)
	return out


def _redact_member(result: dict, policy: dict) -> None:
	for key in _ID_KEYS:
		if result.get(key):
			result[key] = _mask_id(str(result[key])) if policy.get("id_numbers") or key in ("cr_id", "id") else result[key]
	if policy.get("cr_id") and result.get("cr_id"):
		result["cr_id"] = _mask_id(str(result["cr_id"]))
	if policy.get("names"):
		for key in _NAME_KEYS:
			if result.get(key):
				result[key] = "•••"
		for ident in result.get("other_identifications") or []:
			if isinstance(ident, dict) and policy.get("id_numbers") and ident.get("identification_number"):
				ident["identification_number"] = _mask_id(str(ident["identification_number"]))
	for entry in result.get("dependants") or []:
		if not isinstance(entry, dict):
			continue
		for member in entry.get("members") or []:
			if isinstance(member, dict):
				_redact_member(member, policy)
	if result.get("msisdn") and policy.get("phone"):
		result["msisdn"] = _mask_phone(str(result["msisdn"]))
	if result.get("email") and policy.get("email"):
		result["email"] = _mask_email(str(result["email"]))
	if result.get("date_of_birth") and policy.get("dob"):
		result["date_of_birth"] = _mask_dob(str(result["date_of_birth"]))
	if result.get("contributions") and isinstance(result["contributions"], dict):
		result["contributions"]["employer_name"] = "•••" if policy.get("names") else result["contributions"].get("employer_name", "")


def _redact_record(record: dict, policy: dict) -> None:
	if policy.get("names") and record.get("client_name"):
		record["client_name"] = "•••"
	if policy.get("names") and record.get("facility_name"):
		record["facility_name"] = record["facility_name"]  # facility names are not member PII
	if policy.get("cr_id"):
		client = record.get("client")
		if client and str(client) != str(record.get("facility")):
			# ``client`` carries the member CR ID on claims/preauths.
			record["client"] = _mask_id(str(client))
	for key in ("msisdn", "phone"):
		if record.get(key) and policy.get("phone"):
			record[key] = _mask_phone(str(record[key]))
	if record.get("date_of_birth") and policy.get("dob"):
		record["date_of_birth"] = _mask_dob(str(record["date_of_birth"]))
	for procedures in (record.get("procedures") or []):
		if isinstance(procedures, dict) and policy.get("names") and procedures.get("practitioner_name"):
			procedures["practitioner_name"] = "•••"


def _redact_fhir_brief(resource: dict, policy: dict) -> None:
	"""Redact the compact FHIR views (Patient names / DOB / identifiers)."""
	if resource.get("resourceType") == "Patient":
		if policy.get("names"):
			for entry in resource.get("name") or []:
				if isinstance(entry, list) or isinstance(entry, str):
					continue
				if isinstance(entry, dict):
					entry.pop("family", None)
					entry.pop("given", None)
					entry["given"] = ["•••"] if policy.get("names") else []
					entry["family"] = "•••"
		if policy.get("dob") and resource.get("birthDate"):
			resource["birthDate"] = _mask_dob(str(resource["birthDate"]))
		if policy.get("id_numbers"):
			for ident in resource.get("identifier") or []:
				if isinstance(ident, dict) and ident.get("value"):
					ident["value"] = _mask_id(str(ident["value"]))
