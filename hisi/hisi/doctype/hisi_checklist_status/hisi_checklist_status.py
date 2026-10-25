import re

import frappe
from frappe.model.document import Document

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class HISIChecklistStatus(Document):
	"""Configurable final-judgement status for a vendor validation review.

	Statuses are data, not code: the admin API serves only enabled rows and
	accepts a judgement whose status_key matches one of them. Each status
	declares how it is treated (Approve / Reject / Request More Info / Other)
	and whether a reviewer comment is mandatory.

	The workflow graph lives in ``allowed_next``: an organization's payer
	validation status may only move to one of the parent status's allowed
	next statuses (empty list = terminal status).
	"""

	def validate(self):
		self.status_key = (self.status_key or "").strip().lower()
		self.label = (self.label or "").strip()
		if not SLUG_PATTERN.match(self.status_key or ""):
			frappe.throw(
				"Status Key must be a lowercase slug, e.g. approved, rejected, more_info_required.",
				frappe.ValidationError,
			)
		self._validate_transitions()
		self._ensure_at_least_one_enabled()
		self._ensure_at_least_one_start()

	def _validate_transitions(self):
		"""No self-loops, no duplicate targets, targets must be enabled."""
		seen: set[str] = set()
		for row in self.get("allowed_next") or []:
			target = (row.to_status or "").strip()
			if not target:
				frappe.throw("Each allowed next status needs a To Status.", frappe.ValidationError)
			if target == self.name:
				frappe.throw(
					f"Status '{self.label}' cannot transition to itself.",
					frappe.ValidationError,
				)
			if target in seen:
				frappe.throw(
					f"Status '{target}' is listed more than once as an allowed next status.",
					frappe.ValidationError,
				)
			seen.add(target)
			if not frappe.db.get_value("HISI Checklist Status", target, "enabled"):
				frappe.throw(
					f"Allowed next status '{target}' is disabled. Enable it first.",
					frappe.ValidationError,
				)

	def _ensure_at_least_one_enabled(self):
		"""The judgement form must always offer at least one status."""
		if self.enabled:
			return
		filters = {"enabled": 1, "name": ("!=", self.name or "")}
		if not frappe.db.exists("HISI Checklist Status", filters):
			frappe.throw(
				"At least one checklist status must stay enabled.",
				frappe.ValidationError,
			)

	def _ensure_at_least_one_start(self):
		"""New organizations need a defined starting status for the workflow."""
		if self.is_start and self.enabled:
			return
		filters = {"is_start": 1, "enabled": 1, "name": ("!=", self.name or "")}
		if not frappe.db.exists("HISI Checklist Status", filters):
			frappe.throw(
				"At least one enabled status must remain marked as the start status.",
				frappe.ValidationError,
			)
