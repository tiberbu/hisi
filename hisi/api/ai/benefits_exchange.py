# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""The deterministic BenefitsExchange client (the v2 Phase D surface).

Per direction, the API is queried **by intervention code only** — the codes
that appear on claims/preauthorisations — so the result for a code is a
pure function of that code: trivially cacheable per code (TTL from
``HISI Report Settings``), and the static ``HISI Benefit Intervention``
matrix (already code-keyed, same shape) is the fallback/seed.

``get_benefits_profiles`` returns ``{code: profile}`` plus the
``benefits_source`` stamp (``live`` / ``static``) for the job row. Live
results are normalized into the registry row shape (candidate key
matching — the exact wire format is confirmed against the endpoint and
locked in here); a live miss or failure degrades to the static row for
that code, so a slow BenefitsExchange never blocks a report.

The AI is given these structured profiles (never raw API access) and the
investigator stage gets the same single-argument tool.
"""

import json
import time

import frappe
import requests

#: The live query hits ``<endpoint>?<params>&<code_param>=<code>``.
DEFAULT_CODE_PARAM = "code"

#: Candidate wire keys per registry field — the normalizer takes the first
#: present key (the exact names are confirmed against the endpoint and
#: locked in here).
PROFILE_KEYS = {
	"intervention_name": ("intervention_name", "name", "title", "description"),
	"benefit_code": ("benefit_code", "benefit", "package"),
	"sub_benefit": ("sub_benefit", "subbenefit", "subcategory"),
	"complexity": ("complexity",),
	"tariff": ("tariff", "tariff_amount", "rate", "amount", "price"),
	"facility_range": ("facility_range", "facility_levels", "levels", "facility_scale"),
	"payment_mechanism": ("payment_mechanism", "payment_type", "mechanism"),
	"frequency": ("frequency", "limit", "limitation"),
	"mandatory_documents": ("mandatory_documents", "required_documents", "documents"),
	"mandatory_investigations": ("mandatory_investigations", "required_investigations", "investigations"),
}

_PREAUTH_KEYS = ("preauth_required", "requires_preauthorization", "prior_authorization_required", "preauthorization_required")

CACHE_KEY = "hisi:benefits_exchange:{}"


def _cache():
	return frappe.cache()


def _cache_ttl_sec(settings) -> int:
	try:
		return max(1, int(settings.get("benefits_cache_ttl") or 1440)) * 60
	except (TypeError, ValueError):
		return 1440 * 60


def _live_configured(settings) -> bool:
	return bool(str(settings.get("benefits_endpoint") or "").strip())


def _code_param(settings) -> str:
	try:
		params = settings.get("benefits_query_params")
		if isinstance(params, str):
			params = json.loads(params) if params else {}
		if isinstance(params, dict) and params.get("code_param"):
			return str(params["code_param"])
	except (json.JSONDecodeError, ValueError, TypeError):
		pass
	return DEFAULT_CODE_PARAM


def _base_params(settings) -> dict:
	try:
		params = settings.get("benefits_query_params")
		if isinstance(params, str):
			params = json.loads(params) if params else {}
		if isinstance(params, dict):
			return {k: v for k, v in params.items() if k != "code_param"}
	except (json.JSONDecodeError, ValueError, TypeError):
		pass
	return {}


def query_live(code: str, settings, timeout_sec: int = 20) -> dict | None:
	"""One live BenefitsExchange query for a single intervention code.

	Returns a raw payload dict or ``None`` (unreachable / no data). The raw
	payload is normalized by the caller; nothing here decides coverage.
	"""
	if not _live_configured(settings):
		return None
	cache = _cache()
	key = CACHE_KEY.format(code)
	cached = cache.get_value(key)
	if isinstance(cached, dict) and "payload" in cached:
		return None if cached["payload"] is None else cached["payload"]

	endpoint = str(settings.get("benefits_endpoint")).strip()
	params = _base_params(settings)
	params[_code_param(settings)] = code
	headers = {"Accept": "application/json"}
	api_key = settings.get("benefits_api_key")
	if api_key:
		headers["Authorization"] = f"Bearer {api_key}"
	try:
		response = requests.get(endpoint, params=params, headers=headers, timeout=(10.0, timeout_sec))
	except requests.RequestException as exc:
		frappe.log_error(f"BenefitsExchange {code!r} unreachable: {exc}", "BenefitsExchange")
		return None
	if response.status_code >= 400:
		frappe.log_error(
			f"BenefitsExchange {code!r} HTTP {response.status_code}: {response.text[:200]}", "BenefitsExchange"
		)
		return None
	try:
		payload = response.json()
	except (ValueError, json.JSONDecodeError):
		frappe.log_error(f"BenefitsExchange {code!r} returned non-JSON: {response.text[:200]}", "BenefitsExchange")
		return None
	cache.set_value(key, {"payload": payload, "at": int(time.time())}, expires_in=_cache_ttl_sec(settings))
	return payload


def normalize_live_profile(code: str, payload) -> dict | None:
	"""Normalize one live payload into the registry row shape (``None``
	when the payload carries no usable profile — a live miss)."""
	if not isinstance(payload, dict):
		return None
	# Some APIs wrap the profile ({data: {...}} / {result: {...}}).
	node = payload
	for wrapper in ("data", "result", "benefit", "profile"):
		if isinstance(node.get(wrapper), dict):
			node = node[wrapper]
			break
	row = {"intervention_code": code}
	for field, candidates in PROFILE_KEYS.items():
		for key in candidates:
			value = node.get(key)
			if value not in (None, "", [], {}):
				row[field] = value
				break
	preauth_required = False
	for key in _PREAUTH_KEYS:
		if node.get(key) in (None, ""):
			continue
		preauth_required = node.get(key) in (True, 1, "1", "yes", "true", "required")
		break
	if not preauth_required:
		mandatory_docs = str(row.get("mandatory_documents") or "").lower()
		preauth_required = "preauthorization" in mandatory_docs or "pre-auth" in mandatory_docs
	row["preauth_required"] = "Yes" if preauth_required else "No"
	if not any(str(row.get(k) or "").strip() for k in PROFILE_KEYS.values()):
		return None
	return row


def static_profiles(codes: list) -> dict:
	"""The static matrix rows for ``codes`` keyed by code (the fallback/seed)."""
	if not codes:
		return {}
	try:
		rows = frappe.get_all(
			"HISI Benefit Intervention",
			filters={"intervention_code": ["in", codes[:200]]},
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
				"mandatory_documents",
				"mandatory_investigations",
			],
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "static benefits lookup")
		return {}
	return {str(row.intervention_code).strip(): dict(row) for row in rows if row.intervention_code}


def get_benefits_profiles(codes: list, settings=None) -> tuple:
	"""The per-code benefit profiles for one run (live first, static
	fallback). Returns ``(profiles, source, warnings)`` where ``source`` is
	``live`` (any live result), ``static`` or ``none`` (no knowledge at
	all) — the stamp the job row carries."""
	from hisi.api.ai.report_settings import get_report_settings

	settings = settings or get_report_settings()
	codes = [str(c).strip() for c in codes if str(c).strip()]
	if not codes:
		return {}, "none", []
	static = static_profiles(codes) if settings.benefits_mode == "static" else {}
	profiles = dict(static)
	warnings: list = []
	live_hits = 0
	if settings.benefits_mode == "live" and _live_configured(settings):
		for code in codes[:100]:
			payload = query_live(code, settings)
			if payload is None:
				continue
			row = normalize_live_profile(code, payload)
			if row:
				profiles[code] = row
				live_hits += 1
			elif code not in static:
				warnings.append(f"benefits_exchange: live query for {code!r} returned no usable profile")
		if live_hits == 0:
			warnings.append("benefits_exchange: no live results — all codes fell back to the static matrix")
	elif settings.benefits_mode == "live" and not _live_configured(settings):
		warnings.append("benefits_exchange: benefits_mode=live but no endpoint configured — static matrix used")
	source = "live" if live_hits else ("static" if profiles else "none")
	return profiles, source, warnings
