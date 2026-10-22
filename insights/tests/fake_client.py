# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt
"""A recording stand-in for the single Pramaan client.

Tests install it with ``insights.pramaan.set_client`` so no delivery touches a
transport. Every method mirrors the real client's keyword contract.
"""


class FakeAlertClient:
    """Records this app's delivery calls without any transport."""

    def __init__(self, send_error=None):
        self.emails = []
        self.notifications = []
        self.channel_posts = []
        self.workflow_calls = []
        self.send_error = send_error

    def send_email(self, *, to, subject, html=None, text=None, reply_to=None, idempotency_key=None):
        if self.send_error:
            raise self.send_error
        self.emails.append(
            {"to": list(to), "subject": subject, "html": html, "text": text, "reply_to": reply_to}
        )
        return {"status": "queued"}

    def send_notification(self, *, subscriber_id, body, title=None, channel="inbox", idempotency_key=None):
        if self.send_error:
            raise self.send_error
        self.notifications.append({"subscriber_id": subscriber_id, "title": title, "body": body})
        return {"accepted": True, "state": "accepted"}

    def post_to_channel(self, *, account_id, text, idempotency_key=None):
        if self.send_error:
            raise self.send_error
        self.channel_posts.append({"account_id": account_id, "text": text})
        return {"status": "published"}

    def call_workflow(self, *, url, body=None, idempotency_key=None):
        if self.send_error:
            raise self.send_error
        self.workflow_calls.append({"url": url, "body": body})
        return {"status": "succeeded"}
