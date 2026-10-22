"""Production entry point with the framework's permission-aware file serving."""
import os

import frappe.app

# Each local stack owns exactly one site; the host uses a spare localhost port.
frappe.app._site = os.environ.get("BI_SITE_NAME", "bi.localhost")
application = frappe.app.application_with_statics()
