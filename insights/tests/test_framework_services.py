"""Framework account mail cannot fall back to a direct service transport."""
from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase

from insights import framework_services
from insights.pramaan import PramaanClient, PramaanError, set_client


class TestFrameworkServices(IntegrationTestCase):
    def setUp(self):
        self.client = Mock(spec=PramaanClient)
        set_client(self.client)
        self.addCleanup(set_client, None)

    def test_system_mail_preserves_recipients_and_local_template(self):
        with patch("frappe.utils.jinja.get_email_from_template", return_value=("<p>Reset link</p>", "Reset link")):
            framework_services.sendmail(recipients="Reader <reader@example.com>", subject="Reset password",
                template="password_reset", args={"link": "/reset"}, cc=["copy@example.com"], bcc=["hidden@example.com"])
        request = self.client.send_email.call_args.kwargs
        self.assertEqual(request["to"], ["reader@example.com"])
        self.assertEqual(request["cc"], ["copy@example.com"])
        self.assertEqual(request["bcc"], ["hidden@example.com"])
        self.assertIn("Reset link", request["html"])
        self.assertTrue(request["idempotency_key"])

    def test_unknown_delivery_is_not_retried_or_replaced_by_smtp(self):
        self.client.send_email.side_effect = PramaanError("unavailable", uncertain=True)
        self.assertRaises(frappe.OutgoingEmailError, framework_services.sendmail,
            recipients=["reader@example.com"], subject="Reset", message="Reset link")
        self.assertEqual(self.client.send_email.call_count, 1)

    def test_framework_aliases_use_the_same_transport(self):
        import frappe.email
        with patch.object(frappe, "sendmail"), patch.object(frappe.email, "sendmail"):
            framework_services.install_transport()
            self.assertIs(frappe.sendmail, framework_services.sendmail)
            self.assertIs(frappe.email.sendmail, framework_services.sendmail)

    def test_unused_vendor_setup_is_refused(self):
        self.assertRaises(frappe.ValidationError, framework_services.refuse_vendor_setup, None)

    def test_migration_preserves_incomplete_setup_settings(self):
        from insights import migrate
        settings = Mock(language=None, time_zone=None)
        settings.save.side_effect = AssertionError("setup wizard fields are not ready")
        with patch.object(frappe, "get_single", return_value=settings), \
             patch.object(migrate, "create_admin_team"), \
             patch("insights.desk.install_custom_fields"), \
             patch.object(migrate, "sync_standard_workbooks"):
            migrate.after_migrate()
        settings.db_set.assert_called_once_with({"enable_telemetry": 0, "disable_system_update_notification": 1})
        settings.save.assert_not_called()

    def test_system_settings_keep_outside_updates_and_usage_reports_off(self):
        from frappe.utils.change_log import check_for_update
        from frappe.utils.telemetry.pulse.client import is_enabled

        settings = frappe.get_single("System Settings")
        settings.enable_telemetry = 1
        settings.disable_system_update_notification = 0
        settings.save(ignore_permissions=True)
        self.assertEqual(settings.enable_telemetry, 0)
        self.assertEqual(settings.disable_system_update_notification, 1)
        with patch("frappe.utils.change_log.check_release_on_github", side_effect=AssertionError("must stay local")):
            self.assertIsNone(check_for_update())
        with patch.dict(frappe.conf, pulse_api_key="fixture", pulse_force_enabled=False):
            self.assertFalse(is_enabled())
