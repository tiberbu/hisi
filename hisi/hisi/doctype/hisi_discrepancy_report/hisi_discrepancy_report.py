# Copyright (c) 2026, Tiberbu and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class HISIDiscrepancyReport(Document):
	"""One async AI discrepancy-report job for a member (CR ID).

	Lifecycle: ``Queued`` (created by the API) → ``Processing`` (atomic
	claim by the worker) → ``Success`` or ``Failed``. The worker is the sole
	writer of the result fields; ``tag_snapshot`` and ``prompt_snapshot``
	pin the generation-time state so later tag/prompt edits cannot change a
	finished report.
	"""

	pass
