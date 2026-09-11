import unittest
from unittest.mock import patch, MagicMock

import requests

import supabase_store
from supabase_store import StoreError


class PreflightTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict('os.environ', {
            'PAYMENT_BACKEND': 'supabase',
            'SUPABASE_PROJECT_REF': 'ref123',
            'SUPABASE_ACCESS_TOKEN': 'secret-token',
        })
        env.start(); self.addCleanup(env.stop)
        # PYTEST_CURRENT_TEST is set by pytest itself; keep it so require()'s bypass
        # does not accidentally short-circuit these enabled()==True assertions.

    def test_preflight_passes_when_supabase_authenticates(self):
        response = MagicMock(status_code=200)
        response.json.return_value = [{'ok': 1}]
        with patch('requests.post', return_value=response) as post:
            supabase_store.preflight()
        self.assertIn('read-only', post.call_args.args[0])

    def test_preflight_raises_on_401_without_leaking_token(self):
        response = MagicMock(status_code=401, text='{"message":"Unauthorized"}')
        with patch('requests.post', return_value=response):
            with self.assertRaises(StoreError) as ctx:
                supabase_store.preflight()
        self.assertIn('401', str(ctx.exception))
        self.assertNotIn('secret-token', str(ctx.exception))

    def test_preflight_raises_on_connection_error_without_leaking_token(self):
        with patch('requests.post', side_effect=requests.exceptions.ConnectionError('boom')):
            with self.assertRaises(StoreError) as ctx:
                supabase_store.preflight()
        self.assertNotIn('secret-token', str(ctx.exception))

    def test_preflight_raises_when_token_missing(self):
        with patch.dict('os.environ', {'SUPABASE_ACCESS_TOKEN': ''}):
            with self.assertRaises(StoreError):
                supabase_store.preflight()

    def test_preflight_raises_when_backend_not_supabase(self):
        with patch.dict('os.environ', {'PAYMENT_BACKEND': ''}, clear=False):
            with patch.dict('os.environ', {'PYTEST_CURRENT_TEST': ''}):
                with self.assertRaises(StoreError):
                    supabase_store.preflight()


if __name__ == '__main__':
    unittest.main()
