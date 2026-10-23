# Copyright (c) 2023, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from insights.insights.doctype.insights_alert.insights_alert import (
    ALERT_MAX_ROWS,
    InsightsAlert,
    send_alerts,
)
from insights.permission_user import permission_user
from insights.pramaan import PramaanError, PramaanNotAvailable, set_client
from insights.tests.base import InsightsIntegrationTestCase
from insights.tests.factories import (
    as_user,
    create_test_query,
    create_test_workbook,
    create_user,
    delete_users,
)
from insights.tests.fake_client import FakeAlertClient


def message_context(row_count=1):
    return {
        "alert": {"title": "Late invoices"},
        "query": {"title": "Invoices overdue"},
        "count": row_count,
        "rows": [{"invoice": f"INV-{i}"} for i in range(row_count)],
    }


def workflow_alert():
    """An unsaved alert, which is all `send_workflow_alert` reads."""
    doc = frappe.new_doc("Insights Alert")
    doc.title = "Late invoices"
    doc.channel = "Workflow"
    return doc


def set_settings(**values):
    settings = frappe.get_single("Insights Settings")
    was = {key: settings.get(key) for key in values}
    for key, value in values.items():
        settings.set(key, value)
    settings.save(ignore_permissions=True)
    return was


class TestWorkflowPayload(IntegrationTestCase):
    """The payload is a contract with a receiver's parser. Pin its shape."""

    def setUp(self):
        self.client = FakeAlertClient()
        set_client(self.client)
        self.addCleanup(set_client, None)
        self.was = set_settings(workflow_webhook_url="https://example.com/hooks/insights")
        self.addCleanup(set_settings, **self.was)

    def post_one(self, context):
        workflow_alert().send_workflow_alert("**3 invoices are overdue**", context)
        return self.client.workflow_calls[0]

    # @feature alerts.workflow
    def test_payload_includes_a_version(self):
        call = self.post_one(message_context())
        payload = call["body"]
        self.assertEqual(call["url"], "https://example.com/hooks/insights")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["event"], "insights_alert")
        self.assertEqual(payload["message"], "**3 invoices are overdue**")
        self.assertEqual(payload["context"]["alert"], "Late invoices")
        self.assertEqual(payload["context"]["query"], "Invoices overdue")

    # @feature alerts.workflow
    def test_the_target_is_the_configured_workflow_address(self):
        """No bearer secret of its own: the platform holds the connection."""
        call = self.post_one(message_context())
        self.assertNotIn("sekret-token", str(call))

    # @feature alerts.workflow
    def test_rows_are_capped_and_the_cap_is_declared(self):
        payload = self.post_one(message_context(row_count=ALERT_MAX_ROWS + 150))["body"]
        self.assertEqual(len(payload["context"]["rows"]), ALERT_MAX_ROWS)
        self.assertEqual(payload["context"]["count"], ALERT_MAX_ROWS + 150)
        self.assertTrue(payload["context"]["truncated"])

    # @feature alerts.workflow
    def test_a_short_result_is_not_marked_truncated(self):
        payload = self.post_one(message_context(row_count=3))["body"]
        self.assertEqual(len(payload["context"]["rows"]), 3)
        self.assertFalse(payload["context"]["truncated"])

    # @feature alerts.workflow
    def test_an_unset_address_refuses_the_save(self):
        set_settings(workflow_webhook_url="")
        doc = frappe.new_doc("Insights Alert")
        doc.title = "Late invoices"
        doc.channel = "Workflow"
        with (
            patch.object(InsightsAlert, "check_trusted_code"),
            patch.object(InsightsAlert, "evaluate_condition", return_value=False),
        ):
            with self.assertRaises(frappe.ValidationError):
                doc.validate()


class TestChannelFailures(IntegrationTestCase):
    def setUp(self):
        self.client = FakeAlertClient()
        set_client(self.client)
        self.addCleanup(set_client, None)

    # @feature alerts.workflow
    def test_an_unavailable_channel_says_so_plainly(self):
        self.client.send_error = PramaanNotAvailable("workflow.webhook.call")
        set_settings(workflow_webhook_url="https://example.com/hooks/insights")
        with self.assertRaises(frappe.ValidationError) as raised:
            workflow_alert().send_workflow_alert("msg", message_context())
        self.assertIn("not available yet", str(raised.exception))

    # @feature alerts.workflow
    def test_an_uncertain_delivery_does_not_leak_the_internals(self):
        """Any Insights User can reach a send. It gets the contract message,
        not a traceback."""
        self.client.send_error = PramaanError("unavailable", uncertain=True)
        set_settings(workflow_webhook_url="https://example.com/hooks/insights")
        with self.assertRaises(frappe.ValidationError) as raised:
            workflow_alert().send_workflow_alert("msg", message_context())
        self.assertNotIn("10.1.2.3", str(raised.exception))


class TestFailedAlertIsNotRetriedEveryTick(InsightsIntegrationTestCase):
    # `send_alerts` rolls back what it catches, which would take the fixtures
    # with it. Committing them in class setup is what keeps them around.

    @classmethod
    def before_class(cls):
        cls.settings_was = set_settings(workflow_webhook_url="https://example.com/hooks/insights")
        cls.workbook = create_test_workbook("Administrator")
        query = create_test_query("Administrator", cls.workbook.name)
        cls.alert = frappe.get_doc(
            doctype="Insights Alert",
            title="Workflow that is down",
            channel="Workflow",
            query=query.name,
            frequency="Daily",
            condition="q['status'] == 'Open'",
            custom_condition=1,
            message="{{ rows }}",
        ).insert()

    @classmethod
    def after_class(cls):
        frappe.delete_doc("Insights Alert", cls.alert.name, force=True)
        frappe.delete_doc("Insights Workbook", cls.workbook.name, force=True)
        set_settings(**cls.settings_was)

    # @feature alerts.failed-run-recorded
    def test_a_failed_delivery_records_the_run(self):
        """`last_execution` is the cron's start point. Left unset by a failure,
        the alert is due again on the next four-minute tick and a dead receiver
        gets several hundred deliveries a day.

        The condition is stubbed to met: this is about what the scheduler
        records, not about what the query returns.
        """
        self.assertIsNone(self.alert.last_execution)

        set_client(FakeAlertClient(send_error=PramaanError("unavailable", uncertain=True)))
        with (
            patch.object(InsightsAlert, "evaluate_condition", return_value=True),
            patch.object(InsightsAlert, "get_message_context", return_value=message_context()),
        ):
            send_alerts()
        set_client(None)

        self.assertIsNotNone(frappe.db.get_value("Insights Alert", self.alert.name, "last_execution"))

    # @feature alerts.failed-run-recorded
    def test_the_owner_is_told_a_run_failed_and_not_why(self):
        """`send_alerts` mails the owner. The scheduler's session is
        Administrator, so a driver error was written for someone who may
        configure the data source, and it names the host. The error stays in
        the Error Log."""
        frappe.db.set_value("Insights Alert", self.alert.name, "last_execution", None)
        # `send_alerts` rolls back what it catches
        frappe.db.commit()  # nosemgrep
        detail = "Can't connect to MySQL server on '10.0.4.12'"
        client = FakeAlertClient()
        set_client(client)

        with patch.object(InsightsAlert, "evaluate_condition", side_effect=frappe.ValidationError(detail)), \
             patch("insights.insights.doctype.insights_alert.insights_alert.email_of", return_value="owner@unit-mail.company"):
            send_alerts()
        set_client(None)

        mail = client.emails[0]
        self.assertIn(self.alert.title, mail["subject"])
        self.assertNotIn("10.0.4.12", mail["text"])
        self.assertTrue(
            frappe.get_all("Error Log", filters={"method": f"Failed to send alert: {self.alert.name}"})
        )


class ATickRunsEveryAlert(InsightsIntegrationTestCase):
    """`send_alerts` runs every four minutes. Handling a failed alert also
    writes and commits. If that fails, the alerts after it must still run in
    this window."""

    @classmethod
    def before_class(cls):
        cls.settings_was = set_settings(workflow_webhook_url="https://example.com/hooks/insights")
        cls.workbook = create_test_workbook("Administrator", title="Alert Tick Workbook")
        query = create_test_query("Administrator", cls.workbook.name, title="Alert Tick Query")
        cls.alerts = [
            frappe.get_doc(
                doctype="Insights Alert",
                title=f"Alert Tick {index}",
                channel="Workflow",
                query=query.name,
                frequency="Daily",
                condition="q['status'] == 'Open'",
                custom_condition=1,
                message="{{ rows }}",
            )
            .insert()
            .name
            for index in (1, 2)
        ]

    @classmethod
    def after_class(cls):
        for name in cls.alerts:
            frappe.delete_doc("Insights Alert", name, force=True)
        frappe.delete_doc("Insights Workbook", cls.workbook.name, force=True)
        set_settings(**cls.settings_was)

    def tried(self, refusal, aftermath: str) -> list[str]:
        """The alerts of this class that the tick tried, when every send raises
        `refusal` and `aftermath` raises too."""
        tried = []

        def send(alert):
            tried.append(alert.name)
            raise refusal

        module = "insights.insights.doctype.insights_alert.insights_alert"
        with (
            patch.object(InsightsAlert, "is_event_due", return_value=True),
            patch.object(InsightsAlert, "send_alert", autospec=True, side_effect=send),
            patch(f"{module}.{aftermath}", side_effect=frappe.QueryDeadlockError("deadlock")),
        ):
            send_alerts()

        return sorted(name for name in tried if name in self.alerts)

    # @feature alerts.failed-run-recorded
    def test_a_failed_record_of_a_failed_run_does_not_stop_the_tick(self):
        self.assertEqual(
            self.tried(PramaanError("unavailable", uncertain=True), "record_execution"), sorted(self.alerts)
        )

    # @feature alerts.failed-run-recorded
    def test_a_failed_stop_of_a_refused_alert_does_not_stop_the_tick(self):
        from insights.insights.doctype.insights_alert.insights_alert import SendRefused

        self.assertEqual(self.tried(SendRefused("disabled"), "stop"), sorted(self.alerts))


class TestEmailRecipients(InsightsIntegrationTestCase):
    """A recipient list is read at save, and the mail it produces says who sent it."""

    MEMBER = "alert_recipient@test.com"
    OUTSIDER = "someone@external.example.org"

    @classmethod
    def before_class(cls):
        create_user(cls.MEMBER, first_name="Alert", last_name="Recipient", roles="Insights User")
        cls.workbook = create_test_workbook("Administrator", title="Alert Workbook").name
        cls.query = create_test_query("Administrator", cls.workbook, title="Alert Query").name

    @classmethod
    def after_class(cls):
        frappe.delete_doc("Insights Workbook", cls.workbook, force=True)
        delete_users(cls.MEMBER)

    def email_alert(self, recipients):
        doc = frappe.new_doc("Insights Alert")
        doc.title = "Overdue invoices"
        doc.channel = "Email"
        doc.query = self.query
        doc.frequency = "Daily"
        doc.custom_condition = 1
        doc.condition = "q['status'] == 'Open'"
        doc.message = "{{ rows }}"
        doc.recipients = recipients
        return doc

    # @feature alerts.email
    def test_an_address_outside_this_site_is_a_recipient(self):
        """A report goes to a client or an accountant as often as to a colleague."""
        self.assertEqual(self.email_alert(self.OUTSIDER).get_recipients(), [self.OUTSIDER])

    # @feature alerts.email
    def test_a_list_is_split_and_trimmed(self):
        alert = self.email_alert(f" {self.MEMBER} , {self.OUTSIDER} ")
        self.assertEqual(alert.get_recipients(), [self.MEMBER, self.OUTSIDER])

    # @feature alerts.email
    def test_an_empty_list_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self.email_alert("  ,  ").get_recipients()

    # @feature alerts.email
    def test_saving_reads_the_list(self):
        """`send_alerts` logs a send-time error and marks the alert as run, so a
        list checked only at send fails where nobody is looking."""
        with (
            patch.object(InsightsAlert, "evaluate_condition", return_value=True),
            self.assertRaisesRegex(frappe.ValidationError, "not a valid email address"),
        ):
            self.email_alert("not-an-address").insert()


class TestAlertMailIsAttributable(InsightsIntegrationTestCase):
    """The mail includes the marks that say it is an Insights alert."""

    AUTHOR = "alert_author@test.com"

    @classmethod
    def before_class(cls):
        create_user(cls.AUTHOR, first_name="Alert", last_name="Author", roles="Insights User")
        cls.workbook = create_test_workbook("Administrator", title="Mail Workbook").name
        cls.query = create_test_query("Administrator", cls.workbook, title="Mail Query").name

    @classmethod
    def after_class(cls):
        frappe.delete_doc("Insights Workbook", cls.workbook, force=True)
        delete_users(cls.AUTHOR)

    def alert(self, owner=None):
        doc = frappe.new_doc("Insights Alert")
        doc.title = "Overdue invoices"
        doc.channel = "Email"
        doc.query = self.query
        doc.frequency = "Daily"
        doc.custom_condition = 1
        doc.condition = "q['status'] == 'Open'"
        doc.message = "hello"
        doc.recipients = "someone@external.example.org"
        with patch.object(InsightsAlert, "evaluate_condition", return_value=True):
            doc.insert()
        if owner:
            doc.db_set("owner", owner, update_modified=False)
            doc.owner = owner
        self.addCleanup(frappe.delete_doc, "Insights Alert", doc.name, force=True)
        return doc

    # @feature alerts.email
    def test_a_reply_reaches_the_author(self):
        doc = self.alert(owner=self.AUTHOR)
        client = FakeAlertClient()
        set_client(client)
        doc.send_email_alert("hello")
        set_client(None)
        self.assertEqual(client.emails[0]["reply_to"], self.AUTHOR)

    # @feature alerts.email
    def test_an_admin_owned_alert_omits_the_setup_placeholder(self):
        doc = self.alert()
        client = FakeAlertClient()
        set_client(client)
        with patch.object(frappe.db, "get_value", return_value="admin@example.com"):
            doc.send_email_alert("hello")
        set_client(None)
        reply_to = client.emails[0]["reply_to"]
        self.assertIsNone(reply_to)

    # @feature alerts.email
    def test_the_body_names_the_alert_and_the_site(self):
        doc = self.alert(owner=self.AUTHOR)
        body = doc.evaluate_message({"rows": [], "count": 0, "datatable": ""})
        self.assertIn("Overdue invoices", body)
        self.assertIn(frappe.utils.get_url(allow_header_override=False), body)
        self.assertIn(self.AUTHOR, body)

    # @feature alerts.email
    def test_a_title_written_as_markup_stays_text_in_the_footer(self):
        doc = self.alert()
        doc.title = "<script>x</script>"
        body = doc.evaluate_message({"rows": [], "count": 0, "datatable": ""})
        self.assertNotIn("<script>x</script>", body)
        self.assertIn("&lt;script&gt;", body)


ALERT_TODO_PREFIX = "Insights Alert Test"


class AlertOverSeededTodos(InsightsIntegrationTestCase):
    """A query of three todos — two Open, one Closed — for a condition to read."""

    @classmethod
    def before_class(cls):
        cls.delete_todos()
        cls.todos = [
            frappe.get_doc(
                {
                    "doctype": "ToDo",
                    "description": f"{ALERT_TODO_PREFIX} {status} {i}",
                    "status": status,
                }
            )
            .insert(ignore_permissions=True)
            .name
            for status, i in (("Open", 1), ("Open", 2), ("Closed", 3))
        ]
        cls.workbook = create_test_workbook("Administrator", title="Alert Workbook").name
        cls.query = create_test_query(
            "Administrator",
            cls.workbook,
            title="Alert Query",
            operations=[
                {
                    "type": "source",
                    "table": {"type": "table", "data_source": "Site DB", "table_name": "tabToDo"},
                },
                {
                    "type": "filter",
                    "column": {"type": "column", "column_name": "description"},
                    "operator": "contains",
                    "value": ALERT_TODO_PREFIX,
                },
                {
                    "type": "order_by",
                    "column": {"type": "column", "column_name": "description"},
                    "direction": "asc",
                },
            ],
        ).name
        # The query reads tabToDo over the data source's own connection, which cannot see this transaction.
        frappe.db.commit()  # nosemgrep

    @classmethod
    def after_class(cls):
        frappe.delete_doc("Insights Workbook", cls.workbook, force=True)
        cls.delete_todos()
        # before_class committed, so the runner's rollback leaves these rows behind.
        frappe.db.commit()  # nosemgrep

    @staticmethod
    def delete_todos():
        for name in frappe.get_all(
            "ToDo", filters={"description": ["like", f"{ALERT_TODO_PREFIX}%"]}, pluck="name"
        ):
            frappe.delete_doc("ToDo", name, force=True, ignore_permissions=True)

    def make_alert(self, **fields):
        doc = frappe.new_doc("Insights Alert")
        doc.update(
            {
                "title": "Open todos",
                "channel": "Email",
                "query": self.query,
                "frequency": "Daily",
                "custom_condition": 1,
                "condition": "status == 'Open'",
                "message": "hello",
                "recipients": "someone@external.example.org",
                **fields,
            }
        )
        doc.insert()
        self.addCleanup(frappe.delete_doc, "Insights Alert", doc.name, force=True)
        return doc


class TestCondition(AlertOverSeededTodos):
    """What the condition decides, and what a test send overrides."""

    # @feature alerts.condition
    def test_an_alert_fires_when_a_row_meets_its_condition_and_stays_quiet_when_none_does(self):
        met = self.make_alert(condition="status == 'Open'")
        unmet = self.make_alert(condition="status == 'Nowhere'")

        client = FakeAlertClient()
        set_client(client)
        met.send_alert()
        self.assertEqual(client.emails[0]["to"], ["someone@external.example.org"])

        client.emails.clear()
        unmet.send_alert()
        self.assertEqual(client.emails, [])
        set_client(None)

    def assert_refused_with_one_message(self, condition, message):
        frappe.clear_messages()
        with self.assertRaises(frappe.ValidationError) as refusal:
            self.make_alert(condition=condition)

        self.assertEqual(str(refusal.exception), message)
        self.assertEqual([frappe.parse_json(m)["message"] for m in frappe.local.message_log], [message])

    # @feature alerts.condition
    def test_a_condition_that_does_not_parse_refuses_the_save(self):
        self.assert_refused_with_one_message(
            "status ==", "SyntaxError: invalid syntax at line 1, column 10. Expression: status =="
        )

    # @feature alerts.condition
    def test_a_condition_reading_a_missing_column_refuses_the_save_once(self):
        self.assert_refused_with_one_message(
            "state == 'Open'", "NameError: name 'state' is not defined. Expression: state == 'Open'"
        )

    # @feature alerts.condition
    def test_a_condition_that_is_not_true_or_false_refuses_the_save_once(self):
        frappe.clear_messages()
        with self.assertRaises(frappe.ValidationError) as refusal:
            self.make_alert(condition="status")

        self.assertTrue(str(refusal.exception).endswith(". Expression: status"))
        self.assertEqual(len(frappe.local.message_log), 1)

    # @feature alerts.test-send
    def test_a_test_send_delivers_even_when_the_condition_is_not_met_and_when_the_alert_is_not_due(
        self,
    ):
        alert = self.make_alert(condition="status == 'Nowhere'")
        alert.db_set("last_execution", now_datetime(), update_modified=False)
        alert.reload()

        self.assertFalse(alert.is_event_due())

        client = FakeAlertClient()
        set_client(client)
        alert.test_alert()
        set_client(None)

        self.assertEqual(len(client.emails), 1)

    # @feature alerts.message
    def test_the_message_prints_the_field_tokens_and_the_rows_table(self):
        alert = self.make_alert(message="First: {{ rows[0].description }}\n\n{{ rows }}")

        body = alert.evaluate_message(alert.get_message_context())

        self.assertIn(f"First: {ALERT_TODO_PREFIX} Closed 3", body)
        for status, i in (("Open", 1), ("Open", 2), ("Closed", 3)):
            self.assertIn(f"{ALERT_TODO_PREFIX} {status} {i}", body)
        self.assertIn("<table", body)


class TestRowsAnAlertIncludes(AlertOverSeededTodos):
    # @feature alerts.message alerts.workflow
    def test_the_rows_stop_at_the_cap_and_the_count_is_the_whole_result(self):
        """`send_alert` builds one context for every channel. So its count is the
        whole result's, not the page's, and a mail says how many rows it left out."""
        alert = self.make_alert(message="{{ count }} open\n\n{{ rows }}")

        with patch(f"{InsightsAlert.__module__}.ALERT_MAX_ROWS", 2):
            context = alert.get_message_context()

        self.assertEqual(context["count"], 3)
        self.assertEqual(len(context["rows"]), 2)
        body = alert.evaluate_message(context)
        self.assertIn("3 open", body)
        self.assertIn("The first 2 of 3 rows", body)

    # @feature alerts.message alerts.workflow
    def test_the_rows_and_the_count_are_read_when_the_condition_fires(self):
        """`send_alert` builds its message from `get_message_context` after the
        condition, which reads the table fresh. An author's earlier run caches
        the rows and the count, which expire at different times. So the mail
        could show either one stale."""
        query = frappe.get_doc("Insights Query v3", self.query)
        query.execute(page_size=ALERT_MAX_ROWS)
        query.count_rows()

        todo = frappe.get_doc(
            {"doctype": "ToDo", "description": f"{ALERT_TODO_PREFIX} Open 4", "status": "Open"}
        ).insert(ignore_permissions=True)
        # the query reads over the data source's own connection
        frappe.db.commit()  # nosemgrep
        self.addCleanup(frappe.db.commit)  # nosemgrep
        self.addCleanup(frappe.delete_doc, "ToDo", todo.name, force=True, ignore_permissions=True)

        context = self.make_alert().get_message_context()

        self.assertEqual(context["count"], 4)
        self.assertIn(f"{ALERT_TODO_PREFIX} Open 4", [row["description"] for row in context["rows"]])

    # @feature alerts.condition alerts.message
    def test_a_script_query_is_read_fresh_for_the_condition_and_the_count(self):
        """`send_alert` decides on `evaluate_condition` and mails
        `get_message_context`. A script's output is cached separately from the
        SQL over it. So an earlier run could fire the condition and print the
        count from rows the script no longer returns."""
        script = self.make_script_query(
            "results = frappe.get_all('ToDo', "
            f"filters={{'description': ['like', '{ALERT_TODO_PREFIX} Script%']}}, "
            "fields=['description', 'status'])"
        )
        closed = frappe.get_doc(
            {"doctype": "ToDo", "description": f"{ALERT_TODO_PREFIX} Script 1", "status": "Closed"}
        ).insert(ignore_permissions=True)
        self.addCleanup(frappe.delete_doc, "ToDo", closed.name, force=True, ignore_permissions=True)
        alert = self.make_alert(query=script, condition="status == 'Open'")
        # stands for an author's run of the query, or an earlier tick
        frappe.get_doc("Insights Query v3", script).execute()
        self.assertFalse(alert.evaluate_condition())

        opened = frappe.get_doc(
            {"doctype": "ToDo", "description": f"{ALERT_TODO_PREFIX} Script 2", "status": "Open"}
        ).insert(ignore_permissions=True)
        self.addCleanup(frappe.delete_doc, "ToDo", opened.name, force=True, ignore_permissions=True)

        self.assertTrue(alert.evaluate_condition())
        self.assertEqual(alert.get_message_context()["count"], 2)

    def make_script_query(self, code):
        return create_test_query(
            "Administrator",
            self.workbook,
            title="Alert Script Query",
            operations=[{"type": "code", "code": code}],
        ).name


class TestWhoMaySend(AlertOverSeededTodos):
    """Sending mails the author's recipients, so it needs more than read access
    on the alert. The user it runs as is checked again at every send."""

    READER = "alert_reader@test.com"

    @classmethod
    def before_class(cls):
        super().before_class()
        create_user(cls.READER, first_name="Alert", last_name="Reader", roles="Insights User")

    @classmethod
    def after_class(cls):
        super().after_class()
        delete_users(cls.READER)

    # @feature alerts.test-send
    def test_a_collaborator_who_may_only_read_the_alert_cannot_send_it(self):
        """`AlertSetupDialog` calls `test_alert` through `run_doc_method`, which
        checks only read. The desk form calls `send_alert` the same way."""
        from insights.api.workbooks import update_share_permissions

        alert = self.make_alert(condition="status == 'Open'")
        update_share_permissions(self.workbook, [{"user": self.READER, "read": 1, "write": 0}])

        client = FakeAlertClient()
        set_client(client)
        with as_user(self.READER):
            stored = frappe.get_doc("Insights Alert", alert.name)
            self.assertTrue(stored.has_permission("read"))
            for send in (stored.test_alert, stored.send_alert):
                with self.subTest(send.__name__), self.assertRaises(frappe.PermissionError):
                    send()
        set_client(None)

        self.assertEqual(client.emails, [])
        self.assertIsNone(frappe.db.get_value("Insights Alert", alert.name, "last_execution"))

    # @feature alerts.test-send
    def test_an_unsaved_alert_is_sent_by_a_writer_of_its_workbook_only(self):
        """`QueryAlertsDialog` calls `test_alert` through `run_doc_method` on an
        alert it has not saved yet, named `new-alert-…`. A reader of the
        workbook could send the same payload to recipients they choose."""
        from insights.api.workbooks import update_share_permissions

        def send_unsaved(name):
            alert = frappe.new_doc("Insights Alert")
            alert.update(
                {
                    "title": "Open todos",
                    "channel": "Email",
                    "query": self.query,
                    "frequency": "Daily",
                    "custom_condition": 1,
                    "condition": "status == 'Open'",
                    "message": "hello",
                    "recipients": "someone@external.example.org",
                }
            )
            alert.name = name
            client = FakeAlertClient()
            set_client(client)
            with as_user(self.READER):
                alert.test_alert()
            set_client(None)
            return len(client.emails)

        for name in (None, "new-alert-abc123"):
            with self.subTest(name=name):
                update_share_permissions(self.workbook, [{"user": self.READER, "read": 1, "write": 0}])
                with self.assertRaises(frappe.PermissionError):
                    send_unsaved(name)

                update_share_permissions(self.workbook, [{"user": self.READER, "read": 1, "write": 1}])
                self.assertEqual(send_unsaved(name), 1)

    # @feature alerts.enable
    def test_an_edit_by_another_writer_keeps_the_enabler_and_the_form_names_them(self):
        """`AlertSetupDialog` reads `permission_user` through `insights.api.get_doc`
        to show who the alert runs as. Ruling Q11: writers are trusted, so an edit
        does not change it."""
        from insights.api import get_doc
        from insights.api.workbooks import update_share_permissions

        alert = self.make_alert()
        update_share_permissions(self.workbook, [{"user": self.READER, "read": 1, "write": 1}])

        with as_user(self.READER):
            edited = frappe.get_doc("Insights Alert", alert.name)
            edited.recipients = self.READER
            edited.save()
            loaded = get_doc("Insights Alert", alert.name)

        self.assertEqual(loaded["permission_user"], "Administrator")

    # @feature alerts.enable
    def test_an_alert_whose_user_lost_the_query_sends_nothing(self):
        """`send_alerts` runs each alert as the user who enabled it. A share
        revoked since then stops the next send, not only the next save."""
        from insights.api.workbooks import update_share_permissions

        alert = self.make_alert(condition="status == 'Open'")
        # enabled by the reader, who has since lost the workbook
        alert.db_set("permission_user", self.READER, update_modified=False)
        update_share_permissions(self.workbook, [])

        with permission_user(self.READER):
            client = FakeAlertClient()
            set_client(client)
            with self.assertRaises(frappe.PermissionError):
                alert.send_alert()
            set_client(None)

        self.assertEqual(client.emails, [])

    # @feature alerts.enable query.script
    def test_an_alerts_script_reads_as_the_user_who_enabled_it(self):
        """`send_alerts` enters `permission_user` for the enabler and reads the
        rows it mails through `get_message_context`. The scheduler's session is
        Administrator, and the script used to read as the session."""
        query = create_test_query(
            "Administrator",
            self.workbook,
            title="Alert Script Reader",
            operations=[{"type": "code", "code": "results = [{'user': frappe.session.user}]"}],
        ).name
        alert = self.make_alert(query=query, condition="user != ''")

        for runs_as in (self.READER, "Administrator"):
            with self.subTest(runs_as=runs_as), permission_user(runs_as):
                self.assertEqual([row["user"] for row in alert.get_message_context()["rows"]], [runs_as])
        self.assertEqual(frappe.session.user, "Administrator")


class TestSchedule(AlertOverSeededTodos):
    """When an alert is next due."""

    # @feature alerts.create
    def test_a_new_alert_is_due_by_its_frequency_and_a_cron_alert_by_its_expression(self):
        daily = self.make_alert(frequency="Daily")

        daily.last_execution = "2026-01-01 10:00:00"
        self.assertEqual(daily.next_execution, "2026-01-02 00:00:00.000000")
        self.assertTrue(daily.is_event_due())

        daily.last_execution = now_datetime()
        self.assertFalse(daily.is_event_due())

        every_minute = self.make_alert(frequency="Cron", cron_format="* * * * *")

        every_minute.last_execution = add_to_date(now_datetime(), minutes=-2)
        self.assertTrue(every_minute.is_event_due())

        every_minute.last_execution = add_to_date(now_datetime(), minutes=2)
        self.assertFalse(every_minute.is_event_due())


class TestChannelDelivery(AlertOverSeededTodos):
    """The Telegram and Notify channels post to the bindings saved in Settings;
    the platform client is where the test stops."""

    def setUp(self):
        super().setUp()
        self.client = FakeAlertClient()
        set_client(self.client)
        self.addCleanup(set_client, None)

    # @feature alerts.telegram
    def test_a_channel_alert_posts_the_rendered_message_to_the_connected_account(self):
        was = set_settings(telegram_account_id="acct_123", telegram_channel_title="Reports")
        self.addCleanup(set_settings, **was)
        alert = self.make_alert(
            channel="Telegram",
            recipients="",
            message="{{ count }} open todos",
        )

        alert.send_alert(force=True)

        post = self.client.channel_posts[0]
        self.assertEqual(post["account_id"], "acct_123")
        self.assertEqual(post["text"], "3 open todos")

    # @feature alerts.telegram
    def test_a_channel_alert_without_a_connection_refuses_the_save(self):
        set_settings(telegram_account_id="")
        with self.assertRaises(frappe.ValidationError) as raised:
            self.make_alert(channel="Telegram", recipients="", message="{{ rows }}")
        self.assertIn("not set up yet", str(raised.exception))

    # @feature alerts.notify
    def test_an_in_app_alert_sends_to_the_configured_subscriber(self):
        was = set_settings(notify_subscriber_id="sub_42")
        self.addCleanup(set_settings, **was)
        alert = self.make_alert(
            channel="Notify",
            recipients="",
            message="{{ count }} open todos",
        )

        alert.send_alert(force=True)

        note = self.client.notifications[0]
        self.assertEqual(note["subscriber_id"], "sub_42")
        self.assertIn("3 open todos", note["body"])
        self.assertEqual(note["title"], "Open todos")

    # @feature alerts.notify
    def test_an_in_app_alert_without_a_subscriber_refuses_the_save(self):
        set_settings(notify_subscriber_id="")
        with self.assertRaises(frappe.ValidationError) as raised:
            self.make_alert(channel="Notify", recipients="", message="{{ rows }}")
        self.assertIn("not set up yet", str(raised.exception))

    # @feature alerts.workflow
    def test_a_workflow_alert_posts_the_versioned_payload(self):
        was = set_settings(workflow_webhook_url="https://example.com/hooks/insights")
        self.addCleanup(set_settings, **was)
        alert = self.make_alert(
            channel="Workflow",
            recipients="",
            message="{{ count }} open todos",
        )

        alert.send_alert(force=True)

        call = self.client.workflow_calls[0]
        self.assertEqual(call["url"], "https://example.com/hooks/insights")
        self.assertEqual(call["body"]["message"], "3 open todos")
        self.assertEqual(call["body"]["version"], 1)


class TestScheduledReports(InsightsIntegrationTestCase):
    def setUp(self):
        self.client = FakeAlertClient()
        set_client(self.client)
        self.addCleanup(set_client, None)
        self.workbook = create_test_workbook("Administrator", title="Scheduled report workbook")
        self.query = create_test_query("Administrator", self.workbook.name, title="Scheduled report query")

    def report(self):
        return frappe.get_doc(dict(
            doctype="Insights Alert", title="Weekly totals", query=self.query.name,
            scheduled_report=1, channel="Email", recipients="reader@example.com",
            frequency="Weekly", condition="", message="", disabled=0,
        ))

    def test_report_sends_rendered_rows_without_a_condition(self):
        report = self.report().insert()
        context = {**message_context(), "datatable": "<table><tr><td>10</td></tr></table>"}
        with patch.object(InsightsAlert, "get_message_context", return_value=context):
            report.send_alert()
        self.assertEqual(len(self.client.emails), 1)
        self.assertEqual(self.client.emails[0]["to"], ["reader@example.com"])
        self.assertIn("<table>", self.client.emails[0]["html"])
        self.assertIn("Report: Weekly totals", self.client.emails[0]["subject"])
        self.assertIsNotNone(report.last_execution)
        self.assertFalse(report.is_event_due())

    def test_report_requires_email_and_valid_recipients(self):
        report = self.report()
        report.channel = "Notify"
        self.assertRaises(frappe.ValidationError, report.insert)
        report = self.report()
        report.recipients = "bad-address"
        self.assertRaises(frappe.ValidationError, report.insert)

    def test_report_rechecks_query_permission_before_sending(self):
        report = self.report().insert()
        with patch.object(InsightsAlert, "has_query_permission", side_effect=frappe.PermissionError):
            self.assertRaises(frappe.PermissionError, report.send_alert)
        self.assertEqual(self.client.emails, [])

    def test_schedule_uses_selected_frequency_and_rejects_bad_cron(self):
        report = self.report()
        report.cron_format = "0 * * * *"
        report.insert()
        self.assertEqual(report.get_next_execution().weekday(), 6)
        report.frequency = "Cron"
        report.cron_format = "invalid"
        self.assertRaises(frappe.ValidationError, report.save)
