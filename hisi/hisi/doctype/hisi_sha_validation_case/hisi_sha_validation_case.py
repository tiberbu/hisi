import frappe
from frappe.model.document import Document

from hisi.utils import is_platform_user

# Identity fields are fixed when the workflow is created so a case can never
# be re-pointed at another organization's certificate, product or org.
LOCKED_FIELDS = ("case_name", "vendor", "product", "dha_certificate")
# Platform acknowledgement is recorded only by the administrator workspace.
ACKNOWLEDGEMENT_FIELDS = ("acknowledged_by", "acknowledged_on", "acknowledgement_note")


def _norm(value) -> str:
	return str(value) if value is not None else ""


class HISISHAValidationCase(Document):
	# Row-level access is enforced via the has_permission /
	# permission_query_conditions hooks (hisi/permissions.py).
	def validate(self):
		if self.is_new() or is_platform_user(frappe.session.user):
			return
		previous = frappe.db.get_value(
			self.doctype, self.name, (*LOCKED_FIELDS, *ACKNOWLEDGEMENT_FIELDS), as_dict=True
		)
		if not previous:
			return
		for fieldname in (*LOCKED_FIELDS, *ACKNOWLEDGEMENT_FIELDS):
			if _norm(getattr(self, fieldname, None)) != _norm(previous.get(fieldname)):
				setattr(self, fieldname, previous.get(fieldname))
