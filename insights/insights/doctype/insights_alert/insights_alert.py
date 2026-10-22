# Copyright (c) 2023, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
import html
import re
import uuid
from datetime import datetime

import frappe
import pandas as pd
from croniter import croniter
from frappe import _
from frappe.model.document import Document
from frappe.utils import escape_html, get_url, validate_email_address
from frappe.utils.data import get_datetime, get_datetime_str, now_datetime

from insights.insights.doctype.insights_data_source_v3.ibis_utils import FRAPPE_ERRORS
from insights.insights.doctype.insights_data_source_v3.insights_data_source_v3 import (
    db_connections,
)
from insights.permission_user import get_permission_user, may_run_as, permission_user
from insights.permissions import workbook_of
from insights.pramaan import PramaanError, get_client, usable_contact_address
from insights.utils import deep_convert_dict_to_dict

# The payload is a contract with somebody else's code. Version it, so it can
# change without breaking every receiver that already parses it.
WEBHOOK_PAYLOAD_VERSION = 1

# A query with no ceiling on its rows must not become a mail or a POST with no
# ceiling on its body. `count` still reports the real total.
ALERT_MAX_ROWS = 100


class SendRefused(frappe.PermissionError):
    """The user an alert runs as may not send it. A scheduled run then disables the alert."""


class InsightsAlert(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        channel: DF.Literal["Email", "Telegram", "Notify", "Workflow"]
        condition: DF.Code
        cron_format: DF.Data | None
        custom_condition: DF.Check
        disabled: DF.Check
        frequency: DF.Literal["Hourly", "Daily", "Weekly", "Monthly", "Cron"]
        last_execution: DF.Datetime | None
        message: DF.MarkdownEditor | None
        permission_user: DF.Link | None
        query: DF.Link
        recipients: DF.SmallText | None
        title: DF.Data
    # end: auto-generated types

    def validate(self):
        if self.scheduled_report:
            if self.channel != "Email":
                frappe.throw("Scheduled reports use the Email channel.")
        else:
            self.check_trusted_code()
        if self.disabled:
            return

        if self.query:
            self.has_query_permission()

        if self.channel == "Email":
            self.get_recipients()

        if self.channel in ("Telegram", "Notify", "Workflow"):
            self.require_channel_setup()

        try:
            self.evaluate_condition()
        except FRAPPE_ERRORS:
            # the engine told its cause and the expression already
            raise
        except Exception as e:
            frappe.throw(html.escape(f"{type(e).__name__}: {e}. Expression: {self.condition}", quote=False))

        try:
            self.get_next_execution()
        except (ValueError, KeyError):
            frappe.throw("Choose a valid report schedule.")

        self.set_permission_user()

    def set_permission_user(self):
        """Whoever enables the alert is who it runs as.

        The scheduler runs as Administrator, which passes every gate, and
        `validate` sees the query as it is at save. So the owner could point the
        query at a table they cannot read and have the next tick mail the rows
        out. Recording a user here moves the check to send time: the query runs
        under this user whatever it was changed to say.

        Only the enable transition writes it. Anyone with write on the alert's
        query may save the alert, so re-reading it on every save would let an
        edit as small as a title change hand the alert somebody else's row
        access, and mail the result to the list the alert already had.

        `permission_user` is permlevel 1 so no client can write it. Frappe
        resets permlevel fields before it runs `validate`, so this assignment is
        the one that lands.
        """
        if self.is_new() or self.has_value_changed("disabled"):
            self.permission_user = frappe.session.user

    def require_channel_setup(self):
        """A non-email channel needs its delivery target set up once, in Settings."""
        settings = frappe.get_single("Insights Settings")
        missing = {
            "Telegram": ("telegram_account_id", _("Channel alerts are not set up yet. Connect a channel in Settings.")),
            "Notify": ("notify_subscriber_id", _("In-app alerts are not set up yet. Add the notification subscriber in Settings.")),
            "Workflow": ("workflow_webhook_url", _("Workflow delivery is not set up yet. Add the workflow address in Settings.")),
        }[self.channel]
        if not settings.get(missing[0]):
            frappe.throw(missing[1])

    def has_query_permission(self):
        """Whether the user this alert runs as may read its query.

        Checked on save for whoever saves, and on every send for whoever enabled
        it. A share revoked or access lowered since the save must stop the next mail.
        """
        from insights.permissions import can_read_referenced_query

        if not can_read_referenced_query(self.query):
            frappe.throw(_("You do not have permission to access this query"), frappe.PermissionError)

    @frappe.whitelist(methods=["POST"])
    def send_alert(self, force: bool = False):
        # Sending mails the author's recipients, so read access is not enough,
        # and `run_doc_method` and the desk form check only read. Check the user
        # the alert runs as, because the scheduler's session is Administrator.
        # Check the document, not its name: an unsaved alert has no row to load,
        # so its query's workbook decides.
        runs_as = get_permission_user()
        if not may_run_as(self.doctype, self, runs_as):
            if not frappe.db.get_value("User", runs_as, "enabled"):
                frappe.throw(_("{0}, who this alert runs as, is disabled.").format(runs_as), SendRefused)
            workbook = workbook_of(self)
            frappe.throw(
                _("{0}, who this alert runs as, may not edit the workbook {1}.").format(
                    runs_as, frappe.db.get_value("Insights Workbook", workbook, "title") or workbook
                ),
                SendRefused,
            )
        self.has_query_permission()

        results = True if self.scheduled_report else self.evaluate_condition()
        if not results and not force:
            return

        # Built once: the context runs the query, and a non-email alert sends
        # the rendered message and the rows behind it.
        context = self.get_message_context()
        message = self.evaluate_message(context)

        if self.channel == "Email":
            self.send_email_alert(message)
        if self.channel == "Telegram":
            self.send_telegram_alert(message)
        if self.channel == "Notify":
            self.send_notify_alert(message)
        if self.channel == "Workflow":
            self.send_workflow_alert(message, context)

        self.db_set("last_execution", now_datetime(), update_modified=False)

    @staticmethod
    def send_key():
        """One key per delivery attempt. A repeat with the same key cannot
        deliver twice; a new window is a new attempt."""
        return str(uuid.uuid4())

    def send_telegram_alert(self, message):
        """Post the alert to the channel connected in Settings."""
        settings = frappe.get_single("Insights Settings")
        try:
            get_client().post_to_channel(
                account_id=settings.telegram_account_id,
                text=message[:4000],
                idempotency_key=self.send_key(),
            )
        except PramaanError as e:
            frappe.throw(str(e) if e.args else _("Channel delivery failed"))

    def send_notify_alert(self, message):
        """Post the alert as an in-app notification to the configured subscriber."""
        settings = frappe.get_single("Insights Settings")
        try:
            get_client().send_notification(
                subscriber_id=settings.notify_subscriber_id,
                title=self.title,
                body=message[:8000],
                channel="inbox",
                idempotency_key=self.send_key(),
            )
        except PramaanError as e:
            frappe.throw(str(e) if e.args else _("Notification delivery failed"))

    def send_workflow_alert(self, message, context):
        """Hand the alert to the owner's connected workflow account.

        The payload keeps its versioned shape, so a receiver that already
        parses it keeps parsing it. The workflow account does the rest; this
        call ends when the hand-off is accepted, not when the workflow ends.
        """
        settings = frappe.get_single("Insights Settings")
        payload = {
            "version": WEBHOOK_PAYLOAD_VERSION,
            "event": "insights_alert",
            "message": message,
            "context": {
                "alert": context["alert"]["title"],
                "query": context["query"]["title"],
                "count": context["count"],
                "rows": context["rows"][:ALERT_MAX_ROWS],
                "truncated": context["count"] > ALERT_MAX_ROWS,
                "triggered_at": get_datetime_str(now_datetime()),
            },
        }
        try:
            get_client().call_workflow(
                url=settings.workflow_webhook_url,
                body=payload,
                idempotency_key=self.send_key(),
            )
        except PramaanError as e:
            frappe.throw(str(e) if e.args else _("Workflow delivery failed"))

    def get_author_email(self):
        """The reply-to address, or None. A mail without a reply-to is better than no mail."""
        return email_of(self.owner)

    def send_email_alert(self, message):
        """Mail the alert, marked as one.

        The body is written by whoever owns the alert and leaves through the
        platform's mail capability, so the mail names the alert that produced
        it and answers to its author rather than to the site.
        """
        try:
            get_client().send_email(
                to=self.get_recipients(),
                subject=f"Business Intelligence {'Report' if self.scheduled_report else 'Alert'}: {self.title}",
                html=message,
                reply_to=self.get_author_email(),
                idempotency_key=self.send_key(),
            )
        except PramaanError as e:
            frappe.throw(str(e) if e.args else _("Email delivery failed"))

    def check_trusted_code(self):
        """Compare the condition with the stored alert's, not with the one the last
        save saw. A send runs the condition from the request body, saved or not."""
        from insights.permissions import check_trusted_code_author

        stored = frappe.db.get_value(self.doctype, self.name, "condition") if self.name else None
        check_trusted_code_author([(self.title, {"expression": self.condition}, {"expression": stored})])

    def evaluate_condition(self):
        if self.scheduled_report:
            return True
        self.check_trusted_code()
        doc = frappe.get_doc("Insights Query v3", self.query)
        with db_connections():
            return doc.evaluate_alert_expression(self.condition)

    def evaluate_message(self, context):
        if self.scheduled_report:
            return "<h1>" + escape_html(self.title) + "</h1>" + context["datatable"]
        rows_pattern = r"{{\s*rows\s*}}"
        message_md = re.sub(rows_pattern, "{{ datatable }}", self.message)

        message_md = render_template_restricted(message_md, context)
        # A non-email receiver wants the text, not the styled email body.
        if self.channel in ("Telegram", "Notify", "Workflow"):
            return message_md

        message_html = frappe.utils.md_to_html(message_md)
        return frappe.render_template(  # nosemgrep - the template is a file in this app
            "insights/templates/alert.html",
            context=frappe._dict(
                message=message_html,
                # The template renders without autoescaping, so a value
                # interpolated into it is escaped here.
                alert=escape_html(self.title),
                author=escape_html(self.get_author_email() or self.owner),
                site_url=get_url(allow_header_override=False),
            ),
        )

    def get_message_context(self):
        # Read fresh, like the condition that fired. A cached page and a cached
        # count expire at different times, so they may not match it or each other.
        doc = frappe.get_doc("Insights Query v3", self.query)
        with db_connections():
            rows = doc.execute(page_size=ALERT_MAX_ROWS, force=True)["rows"]
            count = doc.count_rows(force=True)

        datatable = pd.DataFrame(rows).to_html(index=False)
        if count > len(rows):
            datatable += "<p>{}</p>".format(_("The first {0} of {1} rows").format(len(rows), count))
        datatable = f"<div class='datatable-container'>{datatable}</div>"
        return deep_convert_dict_to_dict(
            {
                "rows": rows,
                "count": count,
                "query": {
                    "title": doc.title,
                },
                "alert": {
                    "title": self.title,
                },
                "datatable": datatable,
            }
        )

    def get_recipients(self):
        """The addresses this alert mails.

        Read at save as well as at send. `send_alerts` turns a send-time error
        into an Error Log entry and marks the alert as run, so an address
        checked only at send fails where nobody is looking.
        """
        recipients = [address.strip() for address in (self.recipients or "").split(",") if address.strip()]
        if not recipients:
            frappe.throw(_("An email alert needs at least one recipient"))

        for recipient in recipients:
            if not validate_email_address(recipient):
                frappe.throw(_("{0} is not a valid email address").format(recipient))

        return recipients

    @property
    def next_execution(self):
        return get_datetime_str(self.get_next_execution())

    def get_next_execution(self):
        CRON_MAP = {
            "Monthly": "0 0 1 * *",
            "Weekly": "0 0 * * 0",
            "Daily": "0 0 * * *",
            "Hourly": "0 * * * *",
        }
        expression = self.cron_format if self.frequency == "Cron" else CRON_MAP[self.frequency]

        start_time = get_datetime(self.last_execution or datetime(2000, 1, 1))
        return croniter(expression, start_time).get_next(datetime)

    def is_event_due(self):
        if not self.last_execution:
            return True

        next_execution = self.get_next_execution()
        return next_execution <= now_datetime()

    @frappe.whitelist(methods=["POST"])
    def test_alert(self):
        self.send_alert(force=True)


def send_alerts():
    alerts = frappe.get_all("Insights Alert", filters={"disabled": 0}, fields=["name", "permission_user"])
    for alert in alerts:
        # Handling a failed run also writes and commits. If that fails, the
        # alerts after it must still run in this window.
        try:
            run_alert(alert)
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title=f"Failed to record the failed run of alert: {alert.name}")


def run_alert(alert):
    try:
        alert_doc = frappe.get_cached_doc("Insights Alert", alert.name)
        if alert_doc.is_event_due():
            # the scheduler runs as Administrator, so without this the alert
            # would read every row of every table it names
            with permission_user(alert.permission_user):
                alert_doc.send_alert()
        frappe.db.commit()
    except SendRefused as refusal:
        frappe.db.rollback()
        stop(alert.name, str(refusal))
    except Exception:
        frappe.db.rollback()
        frappe.log_error(title=f"Failed to send alert: {alert.name}")
        record_execution(alert.name)
        tell_owner_it_failed(alert.name)


def record_execution(name: str):
    """Mark an alert as run, whether or not it delivered.

    `last_execution` is the cron's start point, so leaving it unset after a
    failure makes the alert due again on the next four-minute tick. A delivery
    pointed at a receiver that is down would be retried forever, several
    hundred times a day, against somebody else's server. The alert ran. The
    next attempt belongs in the next scheduled window.

    This runs after the rollback above, so it needs its own commit.
    """
    frappe.db.set_value("Insights Alert", name, "last_execution", now_datetime(), update_modified=False)
    # The caller rolled back the failed alert. Without a commit of its own this
    # write goes out with the next one, or with nothing at all.
    frappe.db.commit()  # nosemgrep


def stop(name: str, cause: str):
    """Disable an alert that its enabler may no longer send, and tell the workbook's owner why.

    Every later window would fail the same way. So it stays disabled until
    someone enables it, and that person becomes the user it runs as. The
    alert's owner is often the enabler, who may be disabled or removed, so the
    workbook's owner gets the mail.
    """
    frappe.db.set_value("Insights Alert", name, "disabled", 1)
    # the caller rolled back, so this write needs a commit of its own
    frappe.db.commit()  # nosemgrep

    try:
        alert = frappe.get_doc("Insights Alert", name)
        owner = email_of(frappe.db.get_value("Insights Workbook", workbook_of(alert), "owner"))
        if not owner:
            return
        get_client().send_email(
            to=[owner],
            subject=_("Insights Alert stopped: {0}").format(alert.title),
            text=_(
                "The alert {0} has stopped and is now disabled. {1} Enable it again to run it as yourself."
            ).format(escape_html(alert.title), escape_html(cause)),
            idempotency_key=str(uuid.uuid4()),
        )
    except PramaanError:
        frappe.log_error(title=f"Failed to tell the workbook owner that alert {name} stopped")
    except Exception:
        frappe.log_error(title=f"Failed to tell the workbook owner that alert {name} stopped")


def email_of(user: str | None) -> str | None:
    """The address a mail to `user` reaches.

    A User name is an email address for an account created from an invite, and
    the literal "Administrator" for the admin. `sendmail` refuses an address it
    cannot parse, so an unresolved user gets None and the mail still sends.
    """
    email = user and frappe.db.get_value("User", user, "email")
    return email if email and validate_email_address(email) and usable_contact_address(email) else None


def tell_owner_it_failed(name: str):
    """Mail the alert's owner that a scheduled run failed.

    Nobody watches a scheduled run. An alert that stops sending looks the same
    as one whose condition was never met. The next attempt is the next
    scheduled window, so this sends at most one mail per window.

    The mail says that the run failed, not why. The scheduler's session is
    Administrator, so the error was written for someone who may configure the
    data source. A driver error names the host and the account. The error
    stays in the Error Log.
    """
    try:
        alert = frappe.get_doc("Insights Alert", name)
        owner = alert.get_author_email()
        if not owner:
            return
        get_client().send_email(
            to=[owner],
            subject=_("Insights Alert failed: {0}").format(alert.title),
            text=_(
                "The alert {0} could not run. It will try again at its next scheduled time. Ask your administrator for the error."
            ).format(escape_html(alert.title)),
            idempotency_key=str(uuid.uuid4()),
        )
    except PramaanError:
        frappe.log_error(title=f"Failed to tell the owner of alert {name} that it failed")
    except Exception:
        frappe.log_error(title=f"Failed to tell the owner of alert {name} that it failed")


def render_template_restricted(template: str, context: dict) -> str:
    """Render a Jinja template with a restricted sandbox environment.

    Only allows access to explicitly passed context variables and basic filters.
    Does not expose frappe utilities or other globals.

    Uses the same sandboxed environment as frappe.render_template but without
    the get_safe_globals() that would expose frappe internals.
    """
    try:
        from frappe.utils.jinja import _get_jenv

        base_jenv = _get_jenv()
        jenv = base_jenv.overlay()
        jenv.filters = base_jenv.filters.copy()

    except ImportError:
        # fallback for v15
        from frappe.utils.jinja import get_jenv

        jenv = get_jenv()

    compiled_template = jenv.from_string(template)
    return compiled_template.render(context)
