from frappe.model.document import Document


class HISITestRun(Document):
	# Row-level access is enforced via the has_permission /
	# permission_query_conditions hooks (hisi/permissions.py). Mutations
	# from the HISI portal go through hisi.api.* with explicit checks.
	pass
