# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

"""Deterministic rule engine for the HISI discrepancy report pipeline.

The rule engine is the Phase A/B core of the v2 design: findings are
COMPUTED in code from the fetched data (not by the model), every finding
carries structured evidence that is verified against the fetched context
before it may enter a report, and the model (when a mode uses one) is
reduced to narrow, routed, redacted assistance (narrative / investigation).
"""

from hisi.api.ai.rules.context import RuleContext  # noqa: F401
from hisi.api.ai.rules.engine import (  # noqa: F401
	AI_MODES,
	CATALOG_VERSION,
	run_rules,
)
from hisi.api.ai.rules.evidence import (  # noqa: F401
	EvidenceRef,
	verify_findings,
)
from hisi.api.ai.rules.redact import redact_context_for_llm  # noqa: F401
