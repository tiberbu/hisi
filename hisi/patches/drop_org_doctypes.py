"""Drop the legacy organization doctypes (and the retired SHA pipeline) from
the site.

Organizations live on ERPNext Company records and membership on active
Employee rows — migrated first by hisi.patches.orgs_to_erpnext, which runs
before this patch. Child tables go before their parents; any DocField still
pointing at a dropped doctype is removed with it.
"""
import frappe

# Children before parents; the last entries are the two single tables whose
# member child tables were already dropped above.
DOCTYPES = [
	"HISI Payer Organization Member",
	"HISI Organization Member",
	"HISI Case Criterion Step Progress",
	"HISI Case Criterion Assessment",
	"HISI Checklist Criterion Step",
	"HISI Checklist Criterion Sub Step",
	"HISI Checklist Change Log",
	"HISI Checklist Criterion",
	"HISI Checklist Status Transition",
	"HISI Checklist Status",
	"HISI Checklist Version",
	"HISI Criterion",
	"HISI DHA Certification Reference",
	"HISI HMIS Product",
	"HISI SHA Validation Case",
	"HISI Test Run",
	"HISI Payer Organization",
	"HISI Vendor Organization",
]


def _safe(sql, params=None):
	"""Metadata cleanup: the table may not exist on a lean site."""
	try:
		frappe.db.sql(sql, params)
	except Exception:
		pass


def drop_doctype(name):
	if not frappe.db.exists("DocType", name):
		print("  {} not present, skipping".format(name))
		return
	frappe.delete_doc("DocType", name, force=True, ignore_permissions=True)
	# sweep whatever the delete did not cascade (metadata rows + table)
	_safe("DELETE FROM `tabDocField` WHERE parent=%s", name)
	_safe("DELETE FROM `tabDocField` WHERE options=%s", name)
	_safe("DELETE FROM `tabDocPerm` WHERE parent=%s", name)
	_safe("DELETE FROM `tabDocType Link` WHERE parent=%s", name)
	_safe("DELETE FROM `tabDocType Action` WHERE parent=%s", name)
	_safe("DELETE FROM `tabDocType State` WHERE parent=%s", name)
	_safe("DELETE FROM `tabHas Role` WHERE parent=%s", name)
	_safe("DELETE FROM `tabCustomize Form` WHERE doc_type=%s", name)
	_safe("DELETE FROM `tabUser Permission` WHERE allow=%s", name)
	_safe("DELETE FROM `tabDocType` WHERE name=%s", name)
	_safe("DROP TABLE IF EXISTS `tab{}`".format(name))
	print("  dropped {}".format(name))


def execute():
	print("dropping legacy organization/pipeline doctypes")
	for dt in DOCTYPES:
		drop_doctype(dt)
	# the mandate's payer_org column was orphaned by the model sync
	cols = frappe.db.sql("SHOW COLUMNS FROM `tabHISI Surveillance Mandate`", as_dict=True)
	if any(c["Field"] == "payer_org" for c in cols):
		frappe.db.sql_ddl("ALTER TABLE `tabHISI Surveillance Mandate` DROP COLUMN `payer_org`")
		print("  dropped orphan payer_org column from mandate")
	frappe.db.commit()
	remaining = frappe.get_all("DocType", filters={"name": ("in", DOCTYPES)}, pluck="name")
	if remaining:
		raise RuntimeError("doctypes still present: {}".format(remaining))
	orphan_fields = frappe.get_all(
		"DocField", filters={"options": ("in", DOCTYPES)}, fields=["parent", "fieldname"]
	)
	if orphan_fields:
		raise RuntimeError("DocFields still reference dropped doctypes: {}".format(orphan_fields))
	print("done")
