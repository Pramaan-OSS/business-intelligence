"""Settings reads stay local; a completed pairing can bind its delivery target."""
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from insights.api.alert_channels import (
    check_channel_connect,
    disconnect_channel,
    get_alert_delivery,
    save_delivery_targets,
)
from insights.pramaan import PramaanClient, set_client


class TestAlertDeliverySettings(IntegrationTestCase):
    def setUp(self):
        self.client = PramaanClient(api_key="fixture-key", transport=PramaanClient._fake)
        set_client(self.client)
        self.addCleanup(set_client, None)

    def test_saved_targets_are_read_without_contacting_an_outside_service(self):
        settings = frappe.get_single("Insights Settings")
        settings.update({"notify_subscriber_id": "member-42", "workflow_webhook_url": "https://example.com/workflow"})
        settings.save(ignore_permissions=True)
        with patch.object(self.client, "connected_accounts", side_effect=AssertionError("settings must stay local")):
            result = get_alert_delivery()
        self.assertEqual(result["notify_subscriber_id"], "member-42")
        self.assertEqual(result["workflow_webhook_url"], "https://example.com/workflow")

    def test_fake_pairing_finishes_and_can_be_disconnected(self):
        result = check_channel_connect("fixture-code")
        self.assertEqual(result["status"], "connected")
        self.assertEqual(get_alert_delivery()["channel_label"], "Fixture channel")
        disconnect_channel()
        self.assertFalse(get_alert_delivery()["connected"])

    def test_targets_save_through_the_public_endpoint_and_can_be_cleared(self):
        save_delivery_targets("member-5", "https://example.com/workflow")
        self.assertEqual(get_alert_delivery()["notify_subscriber_id"], "member-5")
        save_delivery_targets()
        self.assertEqual(get_alert_delivery()["workflow_webhook_url"], "")
        self.assertRaises(frappe.ValidationError, save_delivery_targets, "", "http://example.com")

    def test_an_unlabelled_channel_does_not_expose_its_account_reference(self):
        with patch.object(self.client, "connect_channel_check", return_value={"status": "connected", "accountId": "private-account-reference"}):
            result = check_channel_connect("fixture-code")
        self.assertEqual(result["channel_label"], "Connected channel")
        self.assertEqual(get_alert_delivery()["channel_label"], "Connected channel")
