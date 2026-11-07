# Field surveillance (case-based) API package: HQ creates and reviews
# surveillance cases, field officers accept them, work through the checklist,
# attach evidence, submit findings; HQ closes or returns them. Every
# transition is written to the case's activity trail.
#
# The endpoint functions live in ``hisi.api.fs.api`` and are re-exported here
# so the whitelisted method paths (hisi.api.fs.<fn>) resolve, matching the
# surveillance package convention.

from hisi.api.fs.api import (
	accept_case,
	close_case,
	create_case,
	decide_reassignment,
	get_case,
	list_cases,
	list_officers,
	list_templates,
	return_for_rework,
	request_reassignment,
	save_findings,
	save_template,
	search_facilities,
	start_fieldwork,
	submit_findings,
	update_step,
	upload_evidence,
)

__all__ = [
	"accept_case",
	"close_case",
	"create_case",
	"decide_reassignment",
	"get_case",
	"list_cases",
	"list_officers",
	"list_templates",
	"return_for_rework",
	"request_reassignment",
	"save_findings",
	"save_template",
	"search_facilities",
	"start_fieldwork",
	"submit_findings",
	"update_step",
	"upload_evidence",
]
