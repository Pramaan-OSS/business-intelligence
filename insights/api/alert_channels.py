# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Alert delivery setup: connect an alert channel and save its targets.

Only Insights Admins configure delivery. The pairing code never becomes a
local credential: the platform holds the connection, and this app keeps only
the binding it reports back.
"""

import frappe
from frappe import _

from insights.decorators import insights_whitelist
from insights.pramaan import MissingPramaanKey, PramaanError, get_client


def _settings():
    return frappe.get_single("Insights Settings")


def _refuse(message: str):
    frappe.throw(message)


@insights_whitelist(role="Insights Admin")
def get_alert_delivery() -> dict:
    settings = _settings()
    return {
        "configured": get_client().configured,
        "connected": bool(settings.telegram_account_id),
        "channel_label": settings.telegram_account_id and (settings.get("telegram_channel_title") or _("Connected channel")),
        "notify_subscriber_id": settings.notify_subscriber_id or "",
        "workflow_webhook_url": settings.workflow_webhook_url or "",
        "accounts": [],
    }


@insights_whitelist(role="Insights Admin", methods=["POST"])
def start_channel_connect() -> dict:
    """Mint a short-lived pairing code. The screen shows it and polls."""
    try:
        offer = get_client().connect_channel_start()
    except MissingPramaanKey as e:
        _refuse(str(e))
    except PramaanError as e:
        _refuse(str(e) if e.args else "The connection could not be started")
    return {
        "code": offer.get("code") or "",
        "bot_username": offer.get("botUsername") or "",
        "instructions": offer.get("instructions") or [],
        "expires_in": offer.get("expiresIn") or 0,
    }


@insights_whitelist(role="Insights Admin", methods=["POST"])
def check_channel_connect(code: str) -> dict:
    """Poll one pairing attempt. When it connects, keep only the binding."""
    try:
        result = get_client().connect_channel_check(code or "")
    except MissingPramaanKey as e:
        _refuse(str(e))
    except PramaanError as e:
        _refuse(str(e) if e.args else "The connection could not be checked")
    status = result.get("status")
    if status == "connected":
        account_id = result.get("accountId") or ""
        if not account_id:
            frappe.throw(_("The channel connected but reported no binding"))
        settings = _settings()
        settings.telegram_account_id = account_id
        settings.set("telegram_channel_title", result.get("chatTitle") or result.get("displayName") or result.get("handle") or "")
        settings.save()
        return {"status": "connected", "channel_label": settings.get("telegram_channel_title") or _("Connected channel")}
    return {"status": status if isinstance(status, str) else "pending"}


@insights_whitelist(role="Insights Admin", methods=["POST"])
def disconnect_channel() -> dict:
    settings = _settings()
    settings.telegram_account_id = ""
    settings.set("telegram_channel_title", "")
    settings.save()
    return {"status": "disconnected"}


@insights_whitelist(role="Insights Admin", methods=["POST"])
def save_delivery_targets(notify_subscriber_id: str = "", workflow_webhook_url: str = "") -> dict:
    """Save only the reviewed delivery targets; no outside call is needed."""
    from urllib.parse import urlsplit
    notify_subscriber_id = notify_subscriber_id.strip()
    workflow_webhook_url = workflow_webhook_url.strip()
    if len(notify_subscriber_id) > 140 or len(workflow_webhook_url) > 140:
        frappe.throw("Delivery targets must be at most 140 characters.")
    if workflow_webhook_url:
        address = urlsplit(workflow_webhook_url)
        if address.scheme != "https" or not address.hostname or address.username or address.password:
            frappe.throw("Use the public https address of your connected workflow.")
    settings = _settings()
    settings.notify_subscriber_id = notify_subscriber_id
    settings.workflow_webhook_url = workflow_webhook_url
    settings.save()
    return {"notify_subscriber_id": notify_subscriber_id, "workflow_webhook_url": workflow_webhook_url}
