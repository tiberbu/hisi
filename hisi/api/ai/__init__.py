# AI endpoints (self-contained API): the discrepancy tag catalog, the async
# discrepancy-report job, and a synchronous chat completion. The endpoint
# functions live in ``hisi.api.ai.api`` and are re-exported here so the
# whitelisted method paths (hisi.api.ai.<fn>) resolve, matching the
# fs/surveillance package convention.

from hisi.api.ai.api import (
	chat,
	generate_discrepancy_report,
	get_discrepancy_report,
	list_discrepancy_reports,
	list_discrepancy_tags,
)

__all__ = [
	"chat",
	"generate_discrepancy_report",
	"get_discrepancy_report",
	"list_discrepancy_reports",
	"list_discrepancy_tags",
]
