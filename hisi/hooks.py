app_name = "hisi"
app_title = "HISI"
app_publisher = "Tiberbu"
app_description = "HISI - SHA claims validation for DHA-certified HMIS"
app_email = "dev@tiberbu.app"
app_license = "mit"

# Roles created automatically on install.
roles = ["HISI Reviewer", "HISI Vendor"]

# Permissions (server-side row scoping: vendor users see their own organization only)
ORG_SCOPED_DOCTYPES = [
	"HISI Vendor Organization",
	"HISI HMIS Product",
	"HISI DHA Certification Reference",
	"HISI SHA Validation Case",
]

has_permission = {doctype: "hisi.permissions.has_permission" for doctype in ORG_SCOPED_DOCTYPES}

permission_query_conditions = {
	doctype: "hisi.permissions.get_permission_query_conditions" for doctype in ORG_SCOPED_DOCTYPES
}

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "hisi",
# 		"logo": "/assets/hisi/logo.png",
# 		"title": "HISI",
# 		"route": "/hisi",
# 		"has_permission": "hisi.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/hisi/css/hisi.css"
# app_include_js = "/assets/hisi/js/hisi.js"

# include js, css files in header of web template
# web_include_css = "/assets/hisi/css/hisi.css"
# web_include_js = "/assets/hisi/js/hisi.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "hisi/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "hisi/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "hisi.utils.jinja_methods",
# 	"filters": "hisi.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "hisi.install.before_install"
# after_install = "hisi.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "hisi.uninstall.before_uninstall"
# after_uninstall = "hisi.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "hisi.utils.before_app_install"
# after_app_install = "hisi.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "hisi.utils.before_app_uninstall"
# after_app_uninstall = "hisi.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "hisi.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "hisi.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"hisi.tasks.all"
# 	],
# 	"daily": [
# 		"hisi.tasks.daily"
# 	],
# 	"hourly": [
# 		"hisi.tasks.hourly"
# 	],
# 	"weekly": [
# 		"hisi.tasks.weekly"
# 	],
# 	"monthly": [
# 		"hisi.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "hisi.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "hisi.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "hisi.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "hisi.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["hisi.utils.before_request"]
# after_request = ["hisi.utils.after_request"]

# Job Events
# ----------
# before_job = ["hisi.utils.before_job"]
# after_job = ["hisi.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"hisi.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

