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

#: The BenefitsExchange (BMX) Intervention resource path under the base
#: host. The API is a pure function of the intervention code (per the
#: endpoint spec): ``?fields=["*"]&filters=[["intervention_code","=",code]]``
#: — so a code's result is trivially cacheable per code.
BMX_INTERVENTION_PATH = "/v1/bmx/resource/Intervention"

#: Candidate wire keys per registry field — the normalizer takes the first
#: present key (tolerant: the exact BMX field names are matched here).
PROFILE_KEYS = {
	"intervention_name": ("intervention_name", "name", "title", "description"),
	"benefit_code": ("benefit_code", "benefit", "package"),
	"sub_benefit": ("sub_benefit", "subbenefit", "subcategory"),
	"complexity": ("complexity",),
	"tariff": ("tariff", "tariff_amount", "rate", "amount", "cap", "cap_amount", "benefit_amount", "unit_amount", "price"),
	"facility_range": ("facility_range", "facility_levels", "levels", "facility_scale"),
	"payment_mechanism": ("payment_mechanism", "payment_type", "mechanism"),
	"frequency": ("frequency", "limit", "limitation"),
	"mandatory_documents": ("mandatory_documents", "required_documents", "documents"),
	"mandatory_investigations": ("mandatory_investigations", "required_investigations", "investigations"),
}

#: Keys that carry the "does this intervention require preauthorization?"
#: flag, in priority order. Not all interventions require preauth — the
#: MISSING_PREAUTH rule only fires when this flag resolves to True.
_PREAUTH_KEYS = (
	"preauth_required",
	"preauthorization_required",
	"requires_preauthorization",
	"prior_authorization_required",
	"authorization_required",
	"preauth",
	"authorization_flag",
)

#: Values that count as "preauth NOT required" (so an explicit negative
#: doesn't get treated as unknown).
_PREAUTH_FALSE_VALUES = ("", "0", "no", "n", "false", "none", "not_required", "not required", "n/a")

CACHE_KEY = "hisi:benefits_exchange:{}"


def _cache():
	return frappe.cache()


def _cache_ttl_sec(settings) -> int:
	try:
		return max(1, int(settings.get("benefits_cache_ttl") or 1440)) * 60
	except (TypeError, ValueError):
		return 1440 * 60


def _live_base_url(settings) -> str:
	"""The BMX host: the configured ``benefits_base_url``, else the site's
	DHA origin (the production base, or the simulation host when UAT
	simulation is on)."""
	from hisi.api import patient as P

	base = str(settings.get("benefits_base_url") or "").strip()
	if base:
		return base.rstrip("/")
	return str(P.dha_origin()).rstrip("/")


def query_live(code: str, settings, timeout_sec: int = 20) -> dict | None:
	"""One live BenefitsExchange query for a single intervention code.

	Auth is the site's DHA JWT — the same ``Authorization: Bearer`` header the
	other DHA APIs use (``consumer_key``/``consumer_secret`` in HISI Settings).
	Returns a raw payload dict or ``None`` (unreachable / no data / error).
	The payload is normalized by the caller; nothing here decides coverage.
	"""
	from hisi.api import patient as P

	cache = _cache()
	key = CACHE_KEY.format(code)
	cached = cache.get_value(key)
	if isinstance(cached, dict) and "payload" in cached:
		return None if cached["payload"] is None else cached["payload"]

	url = _live_base_url(settings) + BMX_INTERVENTION_PATH
	params = {
		"fields": '["*"]',
		"filters": json.dumps([["intervention_code", "=", code]], separators=(",", ":")),
	}
	try:
		payload = P.http_get(url, params, bearer=True)
	except Exception as exc:  # UpstreamError / requests — logged, not fatal
		frappe.log_error(f"BenefitsExchange {code!r} unreachable: {exc}", "BenefitsExchange")
		return None
	if not isinstance(payload, dict):
		frappe.log_error(f"BenefitsExchange {code!r} returned no JSON object", "BenefitsExchange")
		return None
	cache.set_value(key, {"payload": payload, "at": int(time.time())}, expires_in=_cache_ttl_sec(settings))
	return payload


def normalize_live_profile(code: str, payload) -> dict | None:
	"""Normalize one live payload into the registry row shape (``None``
	when the payload carries no usable profile — a live miss)."""
	if not isinstance(payload, dict):
		return None
	# Some APIs wrap the profile: {data: {...}}, {result: {...}}, or
	# {data: [{...}, ...]} — unwrap to the first profile object.
	node = payload
	for _ in range(3):
		for wrapper in ("data", "result", "benefit", "profile"):
			inner = node.get(wrapper)
			if isinstance(inner, dict):
				node = inner
				break
			if isinstance(inner, list) and inner and isinstance(inner[0], dict):
				node = inner[0]
				break
		else:
			break
	row = {"intervention_code": code}
	for field, candidates in PROFILE_KEYS.items():
		for key in candidates:
			value = node.get(key)
			if value not in (None, "", [], {}):
				row[field] = value
				break
	row["preauth_required"] = _preauth_flag(node)
	if not any(str(row.get(k) or "").strip() for k in PROFILE_KEYS):
		return None
	return row


def _preauth_flag(node: dict):
	"""``True``/``False`` when the payload carries a usable preauth flag, else
	``None`` (unknown — the rule then does not fire for the code)."""
	for key in _PREAUTH_KEYS:
		if key not in node or node.get(key) in (None, ""):
			continue
		value = node.get(key)
		if isinstance(value, bool):
			return value
		if isinstance(value, (int, float)):
			return bool(value)
		text = str(value).strip().lower()
		if text in _PREAUTH_FALSE_VALUES:
			return False
		if text in ("1", "yes", "y", "true", "required", "preauth", "preauthorization", "prior_authorization"):
			return True
		return None  # unrecognized value — treat as unknown, not a guess
	# ``authorization_type`` (e.g. "Preauthorization") is a looser signal.
	auth_type = node.get("authorization_type") or node.get("authorization")
	if auth_type:
		text = str(auth_type).strip().lower()
		if text in _PREAUTH_FALSE_VALUES:
			return False
		if "preauth" in text or "authorization" in text:
			return True
	return None


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
				"preauth_required",
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
	static = static_profiles(codes)  # always: seed + per-code live fallback
	profiles = dict(static)
	warnings: list = []
	live_hits = 0
	if settings.benefits_mode == "live":
		for code in codes[:100]:
			payload = query_live(code, settings)
			if payload is None:
				continue  # keep the static row (or nothing) for the code
			row = normalize_live_profile(code, payload)
			if row:
				profiles[code] = row
				live_hits += 1
			else:
				warnings.append(f"benefits_exchange: live query for {code!r} returned no usable profile — static row kept")
		if live_hits == 0:
			warnings.append("benefits_exchange: no live results — all codes fell back to the static matrix")
	source = "live" if live_hits else ("static" if profiles else "none")
	return profiles, source, warnings
