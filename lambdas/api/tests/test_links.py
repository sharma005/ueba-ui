"""Deep links point at the tenant's own log stream, not somebody else's.

`links` was module globals read from the environment at import, which meant one
process could only ever build links for one customer. The module's own docstring
already warned that a wrong link "takes an analyst somewhere that either 404s
or, worse, belongs to a different tenant" — these pin the fix.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serving import links, tenants  # noqa: E402

JIO = tenants.get("jiostar")
DEEL = tenants.get("deel")
TS = 1788169816000


class Configured(unittest.TestCase):
    def test_no_ui_base_means_no_links_at_all(self):
        """Unset is the shipped state, and it must emit nothing rather than guess."""
        lk = links.for_tenant(JIO)
        self.assertFalse(lk.configured())
        self.assertIsNone(lk.finding_url("i-1", TS))
        self.assertIsNone(lk.signin_url(["e-1"], TS))

    def test_env_base_enables_them(self):
        with mock.patch.dict(os.environ,
                             {"CORALOGIX_UI_BASE": "https://team.app.coralogix.in"}):
            self.assertTrue(links.for_tenant(JIO).configured())

    def test_a_trailing_slash_is_trimmed(self):
        with mock.patch.dict(os.environ,
                             {"CORALOGIX_UI_BASE": "https://team.app.coralogix.in/"}):
            self.assertEqual(links.for_tenant(JIO).ui_base,
                             "https://team.app.coralogix.in")


class PerTenantOverride(unittest.TestCase):
    def test_a_suffixed_variable_beats_the_shared_one(self):
        with mock.patch.dict(os.environ, {
            "CORALOGIX_UI_BASE": "https://shared.app.coralogix.in",
            "CORALOGIX_UI_BASE_DEEL": "https://deel.app.coralogix.com",
        }):
            self.assertEqual(links.for_tenant(DEEL).ui_base,
                             "https://deel.app.coralogix.com")
            self.assertEqual(links.for_tenant(JIO).ui_base,
                             "https://shared.app.coralogix.in")

    def test_the_unsuffixed_variable_still_works_alone(self):
        """A single-tenant deployment's existing environment is unchanged."""
        with mock.patch.dict(os.environ,
                             {"CORALOGIX_ANOMALY_APP": "Legacy_App"}):
            self.assertEqual(links.for_tenant(JIO).anomaly_app, "Legacy_App")


class QueryContents(unittest.TestCase):
    """The app/subsystem pair in the query is what scopes it to one customer."""

    def _q(self, url):
        return unquote(url.split("?query=")[1].split("&")[0])

    def test_a_finding_query_names_the_tenants_own_application(self):
        with mock.patch.dict(os.environ,
                             {"CORALOGIX_UI_BASE": "https://t.app.coralogix.in"}):
            jio_q = self._q(links.for_tenant(JIO).finding_url("i-1", TS))
            deel_q = self._q(links.for_tenant(DEEL).finding_url("i-1", TS))
        self.assertIn("AzureAD_Anomaly_Detection", jio_q)
        self.assertIn("iforest-ap2-v3", jio_q)
        self.assertIn("Okta_Anomaly_Detection", deel_q)
        self.assertIn("iforest-eu1-deel", deel_q)
        # The crucial negative: neither query can match the other's stream.
        self.assertNotIn("Okta", jio_q)
        self.assertNotIn("AzureAD", deel_q)

    def test_a_signin_query_names_the_tenants_own_source(self):
        with mock.patch.dict(os.environ,
                             {"CORALOGIX_UI_BASE": "https://t.app.coralogix.in"}):
            jio_q = self._q(links.for_tenant(JIO).signin_url(["e-1", "e-2"], TS))
            deel_q = self._q(links.for_tenant(DEEL).signin_url(["e-1"], TS))
        self.assertIn("'Azure'", jio_q)
        self.assertIn("'AzureAD'", jio_q)
        self.assertIn("'okta'", deel_q)
        self.assertIn("'Okta-audit'", deel_q)

    def test_only_the_first_ten_sample_ids_are_used(self):
        with mock.patch.dict(os.environ,
                             {"CORALOGIX_UI_BASE": "https://t.app.coralogix.in"}):
            q = self._q(links.for_tenant(JIO).signin_url(
                [f"e-{i}" for i in range(25)], TS))
        self.assertIn("'e-9'", q)
        self.assertNotIn("'e-10'", q)

    def test_a_quote_in_an_id_cannot_break_out_of_the_literal(self):
        with mock.patch.dict(os.environ,
                             {"CORALOGIX_UI_BASE": "https://t.app.coralogix.in"}):
            q = self._q(links.for_tenant(JIO).finding_url("i'; drop", TS))
        self.assertIn("\\'", q)


class NoSharedState(unittest.TestCase):
    """The real risk of turning module globals into instances."""

    def test_two_configs_in_one_process_do_not_bleed(self):
        with mock.patch.dict(os.environ,
                             {"CORALOGIX_UI_BASE": "https://t.app.coralogix.in"}):
            a = links.for_tenant(JIO)
            b = links.for_tenant(DEEL)
        self.assertNotEqual(a.anomaly_app, b.anomaly_app)
        self.assertEqual(a.anomaly_app, "AzureAD_Anomaly_Detection")

    def test_link_config_is_frozen(self):
        import dataclasses

        lk = links.for_tenant(JIO)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            lk.anomaly_app = "something else"


class WindowShape(unittest.TestCase):
    def test_the_window_brackets_the_event(self):
        with mock.patch.dict(os.environ,
                             {"CORALOGIX_UI_BASE": "https://t.app.coralogix.in"}):
            url = links.for_tenant(JIO).finding_url("i-1", TS)
        start = url.split("time=from:")[1].split(",")[0]
        end = url.split(",to:")[1]
        self.assertLess(start, end)
        self.assertEqual(links.WINDOW_HOURS, 6)


if __name__ == "__main__":
    unittest.main()
