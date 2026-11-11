# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""Access to the ``HISI AI Settings`` single with documented defaults.

All runtime configuration for the AI endpoints lives in the single — no
env vars. Every documented key is always present in the returned mapping;
the documented default applies whenever the field is unset, even when the
single row does not exist yet. An unset ``llm_base_url`` stays ``None``:
the AI endpoints fail closed on it (``LLM_NOT_CONFIGURED``).
"""

import frappe
from frappe.utils.password import get_decrypted_password

from hisi.api.patient import KNOWN_STEPS

SINGLE = "HISI AI Settings"

#: Documented defaults — applied whenever the corresponding field is unset.
DEFAULTS = {
	"llm_base_url": None,
	"llm_api_key": None,
	"llm_model": "qwen3-27b",
	"llm_idle_timeout_sec": 120,
	"llm_total_timeout_sec": 600,
	"discrepancy_system_prompt": None,
	"discrepancy_data_sources": "consent,contributions,claims,preauth",
	"discrepancy_max_records": 100,
	# ``None`` = use the built-in SHA benefits knowledge
	# (``hisi.api.ai.prompt.DEFAULT_BENEFITS_REFERENCE``) — the same
	# fallback pattern as ``discrepancy_system_prompt``.
	"benefits_reference": None,
}

# Password fields: their stored column values are ``*`` dummies; the real
# value lives Fernet-encrypted in ``__Auth`` and only the password helpers
# see it.
_SECRET_FIELDS = ("llm_api_key",)

# Int fields: coerced to ``int`` (garbage values fall back to the default);
# a stored 0 is treated as unset ("back to the documented default") — no
# documented Int parameter has a valid 0 value.
_INT_FIELDS = (
	"llm_idle_timeout_sec",
	"llm_total_timeout_sec",
	"discrepancy_max_records",
)


def get_ai_settings():
	"""Return all settings as a ``frappe._dict`` with defaults applied.

	Returns exactly the documented keys. Unset fields fall back to
	:data:`DEFAULTS`; Int fields are coerced to ``int`` (garbage values fall
	back to the default). Secrets are decrypted from ``__Auth`` — ``None``
	when not set, never a raw DB value.
	"""
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
		settings[key] = DEFAULTS[key] if coerced == 0 else coerced
	return settings


def get_ai_data_sources() -> list[str]:
	"""The configured data sources as an ordered, de-duplicated list.

	Always a subset of the ``patient_flow`` steps — names outside
	``KNOWN_STEPS`` are dropped (a typo must not silently break the worker).
	An empty/garbage configuration falls back to the documented default set.
	"""
	raw = str(get_ai_settings().discrepancy_data_sources or "")
	out = []
	for part in raw.split(","):
		step = part.strip().lower()
		if step in KNOWN_STEPS and step not in out:
			out.append(step)
	if not out:
		out = [step for step in DEFAULTS["discrepancy_data_sources"].split(",") if step in KNOWN_STEPS]
	return out
