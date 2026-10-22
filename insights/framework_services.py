"""Keep framework mail on the same transport as the product's own mail."""
import mimetypes
import uuid
from email.utils import getaddresses

import frappe

from insights.pramaan import PramaanError, get_client


def addresses(value):
    values = value if isinstance(value, (list, tuple)) else [value or ""]
    return list(dict.fromkeys(address for _, address in getaddresses(values) if address))


def sendmail(recipients=None, sender="", subject="Message", message="", **kwargs):
    """Send an explicitly requested system message once, without a vendor account."""
    if kwargs.get("send_after"):
        frappe.throw("Scheduled system mail is not available yet. Use a query report schedule.")
    text = None
    if kwargs.get("template"):
        from frappe.utils.jinja import get_email_from_template
        message, text = get_email_from_template(kwargs["template"], kwargs.get("args"))
    message = kwargs.get("content") or message
    if kwargs.get("as_markdown"):
        message = frappe.utils.md_to_html(message)
    attachments = []
    for attachment in kwargs.get("attachments") or []:
        if attachment.get("file_url"):
            file = frappe.get_doc("File", {"file_url": attachment["file_url"]})
            file.check_permission("read")
            data, name = file.get_content(), file.file_name
        else:
            data = attachment.get("fcontent") or attachment.get("content")
            name = attachment.get("fname") or attachment.get("filename") or "attachment"
        if isinstance(data, str):
            data = data.encode()
        attachments.append({"name": name, "content": data,
            "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream"})
    reply_to = addresses(kwargs.get("reply_to"))
    try:
        get_client().send_email(to=addresses(recipients), cc=addresses(kwargs.get("cc")),
            bcc=addresses(kwargs.get("bcc")), subject=subject or "Business Intelligence",
            html=message, text=text, reply_to=reply_to[0] if reply_to else None,
            attachments=attachments, idempotency_key=str(uuid.uuid4()))
    except PramaanError as error:
        frappe.throw(str(error), frappe.OutgoingEmailError)
    # System callers need acceptance, not a legacy mail-queue document.
    return None


def refuse_vendor_setup(doc, method=None):
    frappe.throw("Use the app's connected sources and alert delivery settings. Local password sign-in stays available.")


def enforce_local_settings(doc, method=None):
    """The template never opts into outside usage reports or release lookups."""
    doc.enable_telemetry = 0
    doc.disable_system_update_notification = 1


def install_transport(**kwargs):
    import frappe.email
    frappe.sendmail = sendmail
    frappe.email.sendmail = sendmail
    request = getattr(frappe.local, "request", None)
    command = frappe.form_dict.get("cmd", "") if getattr(frappe.local, "form_dict", None) else ""
    if request and ("/frappe.integrations." in request.path or command.startswith("frappe.integrations.")):
        frappe.throw("This connection is not available here. Use the app's connected sources.", frappe.PermissionError)
