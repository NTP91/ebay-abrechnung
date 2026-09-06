import os
import unittest
from unittest.mock import Mock, patch

import audit_case_store


class AuditCaseStoreTests(unittest.TestCase):
    def test_missing_configuration_stops_before_network(self):
        with patch.dict(os.environ, {}, clear=True), patch('requests.post') as post:
            with self.assertRaisesRegex(audit_case_store.AuditStoreError, 'SUPABASE_ACCESS_TOKEN'):
                audit_case_store.load()
        post.assert_not_called()

    def test_only_fixed_readonly_queries_are_used(self):
        replies = []
        for _ in range(3):
            response = Mock(status_code=200)
            response.json.return_value = []
            replies.append(response)
        with patch.dict(os.environ, {'SUPABASE_ACCESS_TOKEN': 'secret', 'SUPABASE_PROJECT_REF': 'ref'}, clear=True), \
             patch('requests.post', side_effect=replies) as post:
            result = audit_case_store.load()
        self.assertEqual(result, {'cases': [], 'partners': [], 'skus': []})
        self.assertEqual(post.call_count, 3)
        for call in post.call_args_list:
            self.assertTrue(call.args[0].endswith('/query/read-only'))
            self.assertNotIn('secret', str(call.kwargs['json']))

    def test_http_error_does_not_expose_response_or_token(self):
        response = Mock(status_code=403)
        with patch.dict(os.environ, {'SUPABASE_ACCESS_TOKEN': 'secret', 'SUPABASE_PROJECT_REF': 'ref'}, clear=True), \
             patch('requests.post', return_value=response):
            with self.assertRaises(audit_case_store.AuditStoreError) as error:
                audit_case_store.load()
        self.assertNotIn('secret', str(error.exception))


class AuditCaseViewTests(unittest.TestCase):
    def test_review_tables_and_filters_render(self):
        from streamlit.testing.v1 import AppTest
        case = {'order_id': 'o1', 'line_item_id': 'l1', 'partner_id': 'MH', 'sku': 'MH / X',
                'title': 'Produkt', 'has_return': True, 'return_reason_de': 'Artikel defekt',
                'buyer_comment': 'Defekt geliefert', 'has_message': True, 'has_dispute': False,
                'has_hold': False, 'has_negative_feedback': False, 'first_event_at': '2026-09-05T10:00:00Z',
                'last_contact_at': '2026-09-05T11:00:00Z', 'case_status': 'offen',
                'is_problem': True,
                'not_as_described': False, 'wrong_item': False, 'defective': True,
                'used_instead_of_new': False, 'opened_used': False, 'empty_consumed': False,
                'incomplete_parts': False, 'wrong_variant': False, 'item_not_received': False,
                'other_complaint': False}
        partner = {'partner_id': 'MH', 'problem_cases': 1, 'returns': 1, 'messages': 1,
                   'disputes': 0, 'holds': 0, 'negative_feedback': 0, **{key: int(key == 'defective') for key in (
                       'not_as_described','wrong_item','defective','used_instead_of_new','opened_used',
                       'empty_consumed','incomplete_parts','wrong_variant','item_not_received','other_complaint')}}
        sku = {'sku': 'MH / X', 'partner_id': 'MH', 'problem_cases': 2, **{key: int(key == 'defective') for key in (
            'not_as_described','wrong_item','defective','used_instead_of_new','opened_used','empty_consumed',
            'incomplete_parts','wrong_variant','item_not_received','other_complaint')}}
        script = 'import trust_risk_ui\ntrust_risk_ui.render_case_check()'
        with patch('trust_risk_ui.load_audit_cases', return_value={'cases': [case], 'partners': [partner], 'skus': [sku]}):
            app = AppTest.from_string(script).run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.multiselect), 2)
        self.assertEqual(len(app.dataframe), 3)


if __name__ == '__main__':
    unittest.main()
