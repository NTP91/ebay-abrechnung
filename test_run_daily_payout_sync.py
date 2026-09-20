import os
import sys
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scripts'))
import run_daily_payout_sync as job
from ebay_readonly import EbayError


class AlreadyRanTodayTest(unittest.TestCase):
    def test_no_state_means_not_run_yet(self):
        with patch('supabase_store.get_json', return_value=(None, 0)):
            self.assertFalse(job.already_ran_today(date(2026, 9, 20)))

    def test_successful_automatic_run_today_is_detected(self):
        doc = {'runs': [{'trigger': 'automatic', 'status': 'success', 'at': '2026-09-20T19:59:30.000Z'}]}
        with patch('supabase_store.get_json', return_value=(doc, 1)):
            self.assertTrue(job.already_ran_today(date(2026, 9, 20)))

    def test_manual_run_today_does_not_count(self):
        doc = {'runs': [{'trigger': 'manual', 'status': 'success', 'at': '2026-09-20T13:10:16.000Z'}]}
        with patch('supabase_store.get_json', return_value=(doc, 1)):
            self.assertFalse(job.already_ran_today(date(2026, 9, 20)))

    def test_failed_automatic_run_today_does_not_count(self):
        doc = {'runs': [{'trigger': 'automatic', 'status': 'failed', 'at': '2026-09-20T19:59:30.000Z'}]}
        with patch('supabase_store.get_json', return_value=(doc, 1)):
            self.assertFalse(job.already_ran_today(date(2026, 9, 20)))

    def test_successful_automatic_run_yesterday_does_not_count_today(self):
        doc = {'runs': [{'trigger': 'automatic', 'status': 'success', 'at': '2026-09-19T19:59:30.000Z'}]}
        with patch('supabase_store.get_json', return_value=(doc, 1)):
            self.assertFalse(job.already_ran_today(date(2026, 9, 20)))

    def test_winter_utc_run_maps_to_correct_berlin_date(self):
        # 2026-01-05 20:59 UTC (CET, UTC+1) is 2026-01-05 21:59 Europe/Berlin.
        doc = {'runs': [{'trigger': 'automatic', 'status': 'success', 'at': '2026-01-05T20:59:00.000Z'}]}
        with patch('supabase_store.get_json', return_value=(doc, 1)):
            self.assertTrue(job.already_ran_today(date(2026, 1, 5)))


class EbayProviderTest(unittest.TestCase):
    def test_raises_when_core_credentials_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(EbayError):
                job.ebay_provider()

    def test_returns_config_when_all_env_vars_present(self):
        env = {
            'EBAY_CLIENT_ID': 'id', 'EBAY_CLIENT_SECRET': 'secret',
            'EBAY_RU_NAME': 'ru', 'EBAY_REFRESH_TOKEN': 'token',
            'EBAY_SIGNING_PRIVATE_KEY': 'key', 'EBAY_SIGNING_JWE': 'jwe',
            'EBAY_SIGNING_EXPIRATION': '1234567890',
        }
        with patch.dict(os.environ, env, clear=True):
            config = job.ebay_provider()
        self.assertEqual(config['client_id'], 'id')
        self.assertEqual(config['signing_jwe'], 'jwe')


if __name__ == '__main__':
    unittest.main()
