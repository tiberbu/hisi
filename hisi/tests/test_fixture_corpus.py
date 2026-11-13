# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""M13 fixture corpus runner — per-rule precision/recall over synthetic
patient contexts.

Each fixture in ``hisi/tests/fixtures/rules/manifest.json`` is a synthetic
context (claims/consents/preauths/eligibility/SHR/registry) plus:

- ``expect_codes``: tag codes that MUST fire in this context.
- ``absent_codes``: tag codes that MUST NOT fire in this context.
- ``params``: rule parameters applied to every tag (e.g. a custom
  ``medication_family``).

The runner executes the full deterministic catalog (all ``rule_type: code``
tags, reserved/off tags excluded) against each fixture and scores every
rule that any fixture mentions:

- TP: expected and fired.  FN: expected but missing.
- FP: fired but not expected (in that fixture).

A zero FP/FN corpus is the regression bar: a rule change that silently
narrows or widens any rule's behavior fails here even when the targeted
unit test passes. Plain unittest — no site, no DB, no network.
"""

import json
import unittest
from pathlib import Path

from hisi.api.ai.rules.context import RuleContext
from hisi.api.ai.rules.engine import run_rules
from hisi.api.ai.seed import DEFAULT_TAGS, RESERVED_TAG_CODES

MANIFEST = Path(__file__).parent / "fixtures" / "rules" / "manifest.json"


def _load_fixtures() -> list:
	manifest = json.loads(MANIFEST.read_text())
	return manifest.get("fixtures") or []


def _build_context(case: dict) -> RuleContext:
	data = case.get("context") or {}
	consents = data.get("consents") or []
	return RuleContext(
		cr_id=str(case.get("cr_id") or "CR-1"),
		# "records" is included (reliable, empty unless the fixture supplies
		# shr_by_visit) so the absence-based SHR rules can run fail-closed.
		sources=["consent", "eligibility", "contributions", "claims", "preauth", "records"],
		errors=data.get("errors") or {},
		eligibility=data.get("eligibility") or {"eligible": True, "status": "active", "scheme": "SHA", "schemes": []},
		consents=consents,
		consent_by_visit={str(c.get("visit_id")): c for c in consents if c.get("visit_id")},
		claims=data.get("claims") or [],
		preauths=data.get("preauths") or [],
		contributions=data.get("contributions"),
		shr_by_visit=data.get("shr_by_visit") or {},
		records=data.get("records") or {"by_visit": {}},
		registry=data.get("registry") or {},
	)


def _code_tags(params: dict) -> list:
	"""The full deterministic catalog as tag rows (DB-free: the seed constant)."""
	rows = []
	for tag_code, label, severity, instruction, rule_key in DEFAULT_TAGS:
		if tag_code in RESERVED_TAG_CODES:
			continue  # reserved/off — not part of the active catalog
		rows.append(
			{
				"tag_code": tag_code,
				"label": label,
				"severity": severity,
				"rule_type": "code",
				"rule_key": rule_key,
				"parameters": json.dumps(params or {}),
				"severity_policy": "fixed",
				"version": 1,
			}
		)
	return rows


def _run_case(case: dict):
	"""Run one fixture; return (fired_codes, run_output)."""
	out = run_rules(_build_context(case), _code_tags(case.get("params") or {}), None)
	fired = {f.get("tag_code") for f in (out["result"].get("findings") or []) if f.get("tag_code")}
	return fired, out


class TestFixtureCorpus(unittest.TestCase):
	def test_all_fixture_expectations(self):
		fixtures = _load_fixtures()
		self.assertGreater(len(fixtures), 0, "fixture manifest is empty")
		failures = []
		for case in fixtures:
			name = case.get("name")
			try:
				fired, _ = _run_case(case)
			except Exception as exc:  # noqa: BLE001 — one bad fixture must not mask the rest
				failures.append(f"{name}: runner error {exc!r}")
				continue
			for code in case.get("expect_codes") or []:
				if code not in fired:
					failures.append(f"{name}: expected {code} but it did not fire (fired: {sorted(fired)})")
			for code in case.get("absent_codes") or []:
				if code in fired:
					failures.append(f"{name}: absent {code} must not fire (fired: {sorted(fired)})")
		self.assertFalse(failures, "\n".join(failures))

	def test_per_rule_precision_recall(self):
		"""Per-rule P/R over the corpus — scored codes only.

		A code is *scored* when at least one fixture declares it (in expect or
		absent). Incidental firings of un-declared codes in other fixtures are
		out of scope for the metric (the corpus scores the behavior it declares,
		not every rule's full blast radius). Every scored code must reach
		P=1.0 and R=1.0.
		"""
		fixtures = _load_fixtures()
		scored: set = set()
		for case in fixtures:
			scored.update(case.get("expect_codes") or [])
			scored.update(case.get("absent_codes") or [])
		stats: dict = {}

		def bump(code, key):
			stats.setdefault(code, {"tp": 0, "fp": 0, "fn": 0})
			stats[code][key] += 1

		for case in fixtures:
			fired, _ = _run_case(case)
			expect = set(case.get("expect_codes") or [])
			absent = set(case.get("absent_codes") or [])
			for code in expect:
				bump(code, "tp" if code in fired else "fn")
			for code in fired & scored - expect:
				# fired here, but declared expected only elsewhere (or in absent)
				if code in absent:
					bump(code, "fp")
				elif code in expect:
					continue
				else:
					bump(code, "fp")

		lines = []
		worst = []
		for code in sorted(stats):
			s = stats[code]
			prec = s["tp"] / (s["tp"] + s["fp"]) if s["tp"] + s["fp"] else 1.0
			rec = s["tp"] / (s["tp"] + s["fn"]) if s["tp"] + s["fn"] else 1.0
			lines.append(f"{code:28s} tp={s['tp']:2d} fp={s['fp']:2d} fn={s['fn']:2d}  P={prec:.2f} R={rec:.2f}")
			if s["fp"] or s["fn"]:
				worst.append(code)
		self.assertFalse(worst, "per-rule precision/recall not 1.0:\n" + "\n".join(lines))
		self.assertTrue(stats, "no rule scored — fixtures mention no codes")
