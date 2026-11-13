# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""Access to the ``HISI Report Settings`` single with documented defaults.

The v2 process configuration (AI mode, model routing, budgets, redaction,
evidence policy, benefits surface). Every documented key is always present
in the returned mapping; the documented default applies whenever the field
is unset, even when the single row does not exist yet.

The default AI mode is ``legacy_llm`` deliberately: migrating a live site
must not silently switch its pipeline behaviour. A site opts in to the new
deterministic pipeline by setting ``default_ai_mode`` (and, optionally,
per-run mode override) — one config write, fully reversible.
"""

import json

import frappe
from frappe.utils.password import get_decrypted_password

SINGLE = "HISI Report Settings"

#: Documented defaults.
DEFAULTS = {
	"default_ai_mode": "legacy_llm",
	"allow_per_run_mode": 0,
	"clinical_model": "baichuan-m2-32b",
	"general_model": "qwen3-27b",
	"stage_model_routing": None,
	"max_input_tokens": 24000,
	"max_llm_calls": 2,
	"redaction_policy": None,
	"evidence_policy": "exclude",
	"severity_overrides": None,
	"benefits_mode": "static",
	"benefits_cache_ttl": 1440,
	"benefits_base_url": None,
}

_VALID_MODES = ("deterministic_only", "deterministic_plus_narrative", "autonomous_full", "legacy_llm")
_VALID_EVIDENCE_POLICIES = ("exclude", "mark")
_VALID_BENEFITS_MODES = ("static", "live")

_SECRET_FIELDS = ()

_INT_FIELDS = ("max_input_tokens", "max_llm_calls", "benefits_cache_ttl")


def get_report_settings():
	"""Return all settings as a ``frappe._dict`` with defaults applied.

	Mirrors :func:`hisi.api.ai.settings.get_ai_settings`: unset fields fall
	back to :data:`DEFAULTS`, Int fields are coerced (garbage/0 → default),
	secrets come from ``__Auth``. Invalid mode/policy values snap to the
	documented default rather than failing a run."""
	row = frappe.db.get_singles_dict(SINGLE) or {}
	settings = frappe._dict()
	for key, default in DEFAULTS.items():
		if key in _SECRET_FIELDS:
			value = get_decrypted_password(SINGLE, SINGLE, key, raise_exception=False)
		else:
			value = row.get(key)
		settings[key] = default if value in (None, "") else value
	for key in _INT_FIELDS:
		try:
			coerced = int(settings[key])
		except (TypeError, ValueError):
			settings[key] = DEFAULTS[key]
			continue
		settings[key] = DEFAULTS[key] if coerced <= 0 else coerced
	mode = str(settings.default_ai_mode or "").strip().lower()
	if mode not in _VALID_MODES:
		settings.default_ai_mode = DEFAULTS["default_ai_mode"]
	evidence_policy = str(settings.evidence_policy or "exclude").strip().lower()
	if evidence_policy not in _VALID_EVIDENCE_POLICIES:
		settings.evidence_policy = "exclude"
	benefits_mode = str(settings.benefits_mode or "static").strip().lower()
	if benefits_mode not in _VALID_BENEFITS_MODES:
		settings.benefits_mode = "static"
	try:
		settings.allow_per_run_mode = int(bool(settings.allow_per_run_mode))
	except (TypeError, ValueError):
		settings.allow_per_run_mode = 0
	return settings


def resolve_ai_mode(requested_mode, settings=None) -> str:
	"""The mode one run uses: the per-run value when allowed, else the
	configured default. Unknown values fall back to the default."""
	settings = settings or get_report_settings()
	if settings.allow_per_run_mode and requested_mode:
		mode = str(requested_mode).strip().lower()
		if mode in _VALID_MODES:
			return mode
	return str(settings.default_ai_mode or DEFAULTS["default_ai_mode"])


def stage_model(stage: str, settings=None) -> str:
	"""The model routed for one stage (the stage -> model table, then the
	class default: clinical stages → clinical_model, everything else →
	general_model)."""
	settings = settings or get_report_settings()
	routing = settings.stage_model_routing
	if routing:
		try:
			table = json.loads(routing) if isinstance(routing, str) else routing
			if isinstance(table, dict):
				model = str(table.get(stage) or "").strip()
				if model:
					return model
		except (json.JSONDecodeError, ValueError):
			pass
	clinical_stages = ("narrative_clinical", "document_extraction", "investigator", "benefits_explanation")
	if stage in clinical_stages:
		return str(settings.clinical_model or DEFAULTS["clinical_model"])
	return str(settings.general_model or DEFAULTS["general_model"])


def redaction_policy(settings=None) -> dict:
	"""The redaction policy as a dict (garbage → the all-on default)."""
	from hisi.api.ai.rules.redact import DEFAULT_POLICY

	settings = settings or get_report_settings()
	raw = settings.redaction_policy
	if raw in (None, ""):
		return dict(DEFAULT_POLICY)
	try:
		parsed = raw if isinstance(raw, dict) else json.loads(raw)
	except (ValueError, TypeError):
		return dict(DEFAULT_POLICY)
	if not isinstance(parsed, dict):
		return dict(DEFAULT_POLICY)
	return {**DEFAULT_POLICY, **{k: bool(v) for k, v in parsed.items()}}
