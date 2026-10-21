app_name = "fuelbuddy_dubai"
app_title = "Fuelbuddy Dubai"
app_publisher = "Lucky"
app_description = "Fuel Buddy Dubai"
app_email = "luckytamrakar.01@gmail.com"
app_license = "mit"
# required_apps = []

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/fuelbuddy_dubai/css/fuelbuddy_dubai.css"
# app_include_js = "/assets/fuelbuddy_dubai/js/fuelbuddy_dubai.js"

# include js, css files in header of web template
# web_include_css = "/assets/fuelbuddy_dubai/css/fuelbuddy_dubai.css"
# web_include_js = "/assets/fuelbuddy_dubai/js/fuelbuddy_dubai.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "fuelbuddy_dubai/public/scss/website"

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
# app_include_icons = "fuelbuddy_dubai/public/icons.svg"

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

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "fuelbuddy_dubai.utils.jinja_methods",
# 	"filters": "fuelbuddy_dubai.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "fuelbuddy_dubai.install.before_install"
# after_install = "fuelbuddy_dubai.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "fuelbuddy_dubai.uninstall.before_uninstall"
# after_uninstall = "fuelbuddy_dubai.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "fuelbuddy_dubai.utils.before_app_install"
# after_app_install = "fuelbuddy_dubai.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "fuelbuddy_dubai.utils.before_app_uninstall"
# after_app_uninstall = "fuelbuddy_dubai.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "fuelbuddy_dubai.notifications.get_notification_config"

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

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# Warehouse capacity (IDEV-3119), ported from the desk Server Scripts "Capacity on Purchase Receipt"
# and "Capacity for Stock entry" (Before Save -> validate). Switch those scripts off on deploy.
_CAPACITY = "fuelbuddy_dubai.warehouse_capacity"
doc_events = {
	"Purchase Receipt": {"validate": f"{_CAPACITY}.validate_purchase_receipt"},
	"Stock Entry": {"validate": f"{_CAPACITY}.validate_stock_entry"},
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"fuelbuddy_dubai.tasks.all"
# 	],
# 	"daily": [
# 		"fuelbuddy_dubai.tasks.daily"
# 	],
# 	"hourly": [
# 		"fuelbuddy_dubai.tasks.hourly"
# 	],
# 	"weekly": [
# 		"fuelbuddy_dubai.tasks.weekly"
# 	],
# 	"monthly": [
# 		"fuelbuddy_dubai.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "fuelbuddy_dubai.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "fuelbuddy_dubai.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "fuelbuddy_dubai.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["fuelbuddy_dubai.utils.before_request"]
# after_request = ["fuelbuddy_dubai.utils.after_request"]

# Job Events
# ----------
# before_job = ["fuelbuddy_dubai.utils.before_job"]
# after_job = ["fuelbuddy_dubai.utils.after_job"]

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
# 	"fuelbuddy_dubai.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

