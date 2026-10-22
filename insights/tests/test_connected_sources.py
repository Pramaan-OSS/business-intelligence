"""Connected reads use the member binding and preserve complete local snapshots."""
import os
from unittest.mock import patch

import frappe
import insights
from frappe.tests import IntegrationTestCase

from insights.api import connected_sources as sources
from insights.insights.doctype.insights_data_source_v3.insights_data_source_v3 import after_request
from insights.pramaan import PramaanClient, PramaanNotAvailable, set_client


class TestConnectedSources(IntegrationTestCase):
    def setUp(self):
        self.client = PramaanClient(api_key="test-key", transport=PramaanClient._fake)
        set_client(self.client)
        self.addCleanup(set_client, None)
        self.addCleanup(after_request)
        frappe.set_user("Administrator")

    def create_source(self):
        name = sources.create("Connected test " + frappe.generate_hash(length=8), "fixture_sheets", "/records", "rows")
        return frappe.get_doc("Insights Data Source v3", name)

    def test_connect_uses_private_member_reference_and_standard_consent(self):
        with patch.object(self.client, "call", return_value={"connectUrl": "https://example.com/connect"}) as call:
            sources.connect("fixture_sheets")
        inputs = call.call_args.args[1]
        self.assertNotIn("access", inputs)
        self.assertEqual(inputs["endUserRef"], sources.member_ref("Administrator"))
        self.assertNotIn("Administrator", inputs["endUserRef"])
        self.assertNotEqual(inputs["endUserRef"], sources.member_ref("another@example.com"))

    def test_snapshot_is_queryable_and_hourly_refresh_replaces_rows(self):
        doc = self.create_source()
        self.assertEqual(doc.connected_user, "Administrator")
        self.assertEqual(doc.get_ibis_table("records").count().execute(), 2)
        with patch.object(self.client, "read_app", return_value={"status": 200, "body": {"rows": [{"name": "updated", "amount": 8}]}}) as read:
            from insights.insights.doctype.insights_data_source_v3.data_warehouse import execute_warehouse_table_import
            execute_warehouse_table_import(doc.name, "records")
        self.assertTrue(read.called)
        self.assertEqual(insights.warehouse.get_table(doc.name, "records").get_ibis_table().count().execute(), 1)
        self.assertEqual(frappe.db.get_value("Insights Table v3", {"data_source": doc.name}, "stored"), 1)
        self.assertEqual(read.call_args.kwargs["end_user_ref"], sources.member_ref("Administrator"))
        self.assertEqual(doc.get_ibis_table("records").count().execute(), 1)
        self.assertTrue(frappe.db.exists("Insights Table v3", {"data_source": doc.name, "table": "records"}))

    def test_incomplete_and_unavailable_reads_keep_previous_rows(self):
        doc = self.create_source()
        with patch.object(self.client, "read_app", return_value={"status": 200, "body": {"rows": []}, "truncated": True}):
            self.assertRaises(frappe.ValidationError, sources.sync, doc.name)
        with patch.object(self.client, "read_app", side_effect=PramaanNotAvailable("apps.request.read")):
            with self.assertRaisesRegex(frappe.ValidationError, "not available yet"):
                sources.sync(doc.name)
        self.assertEqual(doc.get_ibis_table("records").count().execute(), 2)

    def test_empty_snapshot_clears_previous_rows(self):
        doc = self.create_source()
        with patch.object(self.client, "read_app", return_value={"status": 200, "body": {"rows": []}}):
            sources.sync(doc.name)
        self.assertEqual(doc.get_ibis_table("records").count().execute(), 0)

    def test_disabled_member_cannot_refresh(self):
        doc = self.create_source()
        with patch.object(frappe.db, "get_value", return_value=0), patch.object(self.client, "read_app") as read:
            self.assertRaises(frappe.PermissionError, sources.sync_source, doc)
        read.assert_not_called()

    def test_unsafe_and_oversized_reads_are_refused(self):
        for body in [list(range(1001)), [{"nested": {"secret": 1}}], "text"]:
            self.assertRaises(frappe.ValidationError, sources.checked_rows, {"status": 200, "body": body}, "")
        for path in ["https://example.com", "//example.com", "/records?token=secret"]:
            self.assertRaises(frappe.ValidationError, sources.create, "Unsafe", "fixture_sheets", path)
