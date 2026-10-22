"""Drafts include bounded stored columns and never execute the returned SQL."""
from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase

from insights.api import draft
from insights.pramaan import PramaanClient, set_client


class TestDraft(IntegrationTestCase):
    def setUp(self):
        self.client = Mock(spec=PramaanClient)
        self.client.draft_text.return_value = {"text": "SELECT COUNT(*) FROM orders", "finishReason": "stop"}
        set_client(self.client)
        self.addCleanup(set_client, None)

    def test_schema_uses_stored_column_names_and_limits_context(self):
        tables = {str(i): frappe._dict(table=f"orders_{i}", columns=[
            {"name": f"column_{j}", "type": "Integer"} for j in range(25)
        ]) for i in range(15)}
        with patch.object(draft, "get_stored_columns", return_value=tables):
            schema = draft._schema_context("fixture")
        self.assertIn("column_0 (Integer)", schema)
        self.assertIn("column_19 (Integer)", schema)
        self.assertNotIn("column_20 (Integer)", schema)
        self.assertEqual(len(schema.splitlines()), 12)
        self.assertIn("first 20 columns", schema)

    def test_sql_requires_read_permission_before_generating(self):
        with patch.object(draft, "check_data_source_permission", side_effect=frappe.PermissionError):
            self.assertRaises(frappe.PermissionError, draft.draft_sql, "fixture", "Count orders")
        self.client.draft_text.assert_not_called()

    def test_uncached_schema_still_checks_table_permissions(self):
        tables = [frappe._dict(name="allowed", table_name="orders"), frappe._dict(name="denied", table_name="private")]
        readable = Mock()
        readable.schema.return_value = "fixture-schema"
        with patch.object(draft, "get_stored_columns", return_value={}), \
                patch.object(draft, "get_data_source_tables", return_value=tables), \
                patch.object(draft, "get_permitted_ibis_table", side_effect=[readable, frappe.PermissionError]), \
                patch.object(draft, "get_columns_from_schema", return_value=[{"name": "order_id", "type": "Integer"}]):
            schema = draft._schema_context("fixture")
        self.assertEqual(schema, "table orders: order_id (Integer)")

    def test_empty_questions_and_unknown_title_kinds_are_readable_errors(self):
        self.assertRaises(frappe.ValidationError, draft.draft_sql, "fixture", "  ")
        self.assertRaises(frappe.ValidationError, draft.draft_title, "unknown", "Count orders")
        self.client.draft_text.assert_not_called()

    def test_sql_returns_a_draft_without_executing_it(self):
        with patch.object(draft, "check_data_source_permission") as check_permission, \
                patch.object(frappe.db, "get_value", return_value=frappe._dict(database_type="MariaDB", title="Demo")), \
                patch.object(draft, "_schema_context", return_value="table orders: order_id (Integer)"):
            result = draft.draft_sql("fixture", "Count orders")
        check_permission.assert_called_once_with("fixture")
        self.assertTrue(result["complete"])
        self.assertEqual(result["sql"], "SELECT COUNT(*) FROM orders")
        self.assertIn("order_id (Integer)", self.client.draft_text.call_args.kwargs["user"])
