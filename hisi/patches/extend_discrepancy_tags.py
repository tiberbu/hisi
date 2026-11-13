# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""One-time: give the pre-v2 discrepancy tags their deterministic rule
metadata (runs after the doctype migration that adds ``rule_type``,
``rule_key``, ``severity_policy`` and ``version``).

Built-in tags get ``rule_type=code`` + their evaluator key; unknown
operator-created tags get ``rule_type=off`` (safe default: they never ran
a deterministic rule, so they are skipped by the rule engine — legacy
modes still use their instruction text). Every touched tag's ``version``
is bumped. The new ``TRI_*`` triangulation tags are seeded via
``ensure_default_tags`` (idempotent).
"""

import frappe

from hisi.api.ai.rules.engine import EVALUATORS
from hisi.api.ai.seed import ensure_default_tags

TAG = "HISI Discrepancy Tag"


def execute():
	ensure_default_tags()
	rows = frappe.get_all(TAG, fields=["name", "tag_code", "rule_key", "rule_type", "version"])
	touched = 0
	for row in rows:
		key = str(row.rule_key or "").strip()
		if not key:
			default_key = str(row.tag_code or "").strip().lower()
			if default_key not in EVALUATORS:
				# Operator-created tag with no evaluator: keep it out of the
				# deterministic engine (legacy modes still use its text).
				frappe.db.set_value(TAG, row.name, {"rule_type": "off", "version": (row.version or 1) + 1})
			else:
				frappe.db.set_value(
					TAG, row.name, {"rule_key": default_key, "rule_type": "code", "version": (row.version or 1) + 1}
				)
		elif str(row.rule_type or "code") not in ("code", "llm", "off"):
			frappe.db.set_value(TAG, row.name, {"rule_type": "code", "version": (row.version or 1) + 1})
		touched += 1
	if not frappe.flags.in_test:
		frappe.db.commit()
	frappe.logger("hisi.ai").info(f"extend_discrepancy_tags: reviewed {touched} tag(s)")
