import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

from ebay_readonly import Client, EbayError
import trust_risk as risk


def response(data, status=200):
    result = Mock(status_code=status, headers={})
    result.json.return_value = data
    return result


def config():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return dict(client_id='fake-client', client_secret='fake-secret', ru_name='fake-ru', refresh_token='fake-refresh', signing_private_key=key, signing_jwe='a.b.c.d.e', signing_expiration=9999999999)


class TransportTests(unittest.TestCase):
    def make(self, replies):
        session = Mock()
        session.request.side_effect = replies
        clock = Mock(return_value=1000)
        return Client(config, session, clock), session, clock

    def token(self):
        return response({'access_token': 'fake-access', 'expires_in': 3600})

    def test_token_reuse_refresh_expiry_and_readonly_hosts(self):
        client, session, clock = self.make([self.token(), response({}), response({}), self.token(), response({})])
        client.get('funds')
        client.get('returns')
        clock.return_value = 5000
        client.get('payout', risk.PAYOUT)
        calls = session.request.call_args_list
        self.assertEqual([c.args[0] for c in calls], ['POST', 'GET', 'GET', 'POST', 'GET'])
        self.assertIn('apiz.ebay.com', calls[1].args[1])
        self.assertEqual(calls[2].kwargs['headers']['Authorization'], 'IAF fake-access')
        self.assertTrue(all(c.kwargs['allow_redirects'] is False for c in calls))
        self.assertTrue(all('/identity/v1/oauth2/token' in c.args[1] for c in calls if c.args[0] == 'POST'))
        with self.assertRaises(EbayError):
            client.get('refund')

    def test_401_refresh_once(self):
        client, session, _ = self.make([self.token(), response({}, 401), self.token(), response({}, 401)])
        with self.assertRaisesRegex(EbayError, '401'):
            client.get('funds')
        self.assertEqual(session.request.call_count, 4)

    def test_errors_do_not_leak_response_or_secrets(self):
        for status in (400, 403, 429, 500):
            client, _, _ = self.make([response({'message': 'fake-secret'}, status)])
            with self.assertRaises(EbayError) as failure:
                client.get('funds')
            self.assertNotIn('fake-secret', str(failure.exception))
        client, _, _ = self.make([requests.ConnectionError('fake-secret')])
        with self.assertRaises(EbayError) as failure:
            client.get('funds')
        self.assertNotIn('fake-secret', str(failure.exception))

    def test_redaction(self):
        client, _, _ = self.make([self.token(), response({'memo': 'fake-access fake-secret', 'refresh_token': 'fake-refresh', 'amount': {'value': '1'}})])
        data = client.get('funds')
        self.assertNotIn('fake-', json.dumps(data))
        self.assertNotIn('refresh_token', data)
        self.assertEqual(data['amount']['value'], '1')

    def test_pagination_complete_and_repeated_page_rejected(self):
        client, _, _ = self.make([self.token(), response({'transactions': [{'transactionId': 'a', 'transactionType': 'SALE'}], 'total': 2}), response({'transactions': [{'transactionId': 'b', 'transactionType': 'SALE'}], 'total': 2})])
        self.assertEqual(len(client.pages('transactions', 'transactions')['items']), 2)
        client, _, _ = self.make([self.token(), response({'members': [{'returnId': '1'}], 'paginationOutput': {'totalEntries': 2}}), response({'members': [{'returnId': '1'}], 'paginationOutput': {'totalEntries': 2}})])
        with self.assertRaisesRegex(EbayError, 'wiederholt'):
            client.pages('returns', 'members')

    def test_missing_collection_is_not_zero(self):
        client, _, _ = self.make([self.token(), response({})])
        with self.assertRaisesRegex(EbayError, 'Ergebnisliste'):
            client.pages('disputes', 'paymentDisputes')


def snapshot(**data):
    return {'version': 1, 'fetched_at': '2026-09-04T10:00:00+00:00', 'resources': {key: {'available': True, 'data': val} for key, val in data.items()}}


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = pd.DataFrame([
            {'Bestellnummer': 'order1', 'Partner': 'MH', 'SKU': 'MH43 / 1', 'Produkttitel': 'Vollständiger Titel'},
            {'Bestellnummer': 'multi', 'Partner': 'NB', 'SKU': 'NB / 2', 'Produkttitel': 'Zwei'},
            {'Bestellnummer': 'multi', 'Partner': 'BA', 'SKU': 'BA / 3', 'Produkttitel': 'Drei'}])
        self.orders = pd.DataFrame([
            {'Bestellnummer': 'multi', 'Artikelnummer': 'item2', 'Transaktionsnummer': 'tx2', 'SKU': 'NB / 2'}])

    def test_links_do_not_guess_multi_partner_orders(self):
        self.assertEqual(risk.link_order('multi', {}, self.catalogue, self.orders)['Partner'], 'Nicht zugeordnet')
        self.assertEqual(risk.link_order('multi', {'itemId': 'item2'}, self.catalogue, self.orders)['Partner'], 'NB')
        self.assertEqual(risk.link_order('multi', {'itemId': 'bad'}, self.catalogue, self.orders)['Partner'], 'Nicht zugeordnet')

    def test_local_deadline_priority_partner_text_closed_excluded(self):
        data = snapshot(returns={'items': [{'returnId': 'r1', 'orderId': 'order1', 'state': 'OPEN', 'creationInfo': {'reason': 'NOT_AS_DESCRIBED'}, 'sellerResponseDue': {'respondByDate': {'value': '2026-09-03T23:30:00Z'}}}]}, disputes={'items': [{'paymentDisputeId': 'd1', 'paymentDisputeStatus': 'CLOSED'}, {'paymentDisputeId': 'd2', 'paymentDisputeStatus': 'ACTION_NEEDED', 'orderId': 'order1'}]})
        report = risk.audit(data, self.catalogue, self.orders, datetime(2026, 9, 4, 10, tzinfo=timezone.utc))
        self.assertEqual(len(report['cases']), 2)
        self.assertEqual(report['critical'], 1)
        self.assertEqual(report['today'], 1)
        self.assertEqual(report['cases'][0]['Frist'], '04.09.2026 01:30')
        self.assertIn('NOT_AS_DESCRIBED', report['partners']['MH'])
        self.assertEqual(report['repeated']['MH43 / 1'], 2)

    def test_cache_is_separate_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as folder:
            sentinel = Path(folder) / 'Settlement_Locks.json'
            sentinel.write_text('keep')
            data = snapshot(funds={'fundsOnHold': {'value': '20', 'currency': 'EUR'}})
            risk.save_snapshot(folder, data)
            self.assertEqual(risk.load_snapshot(folder), data)
            self.assertEqual(sentinel.read_text(), 'keep')

    def test_collection_failure_is_unavailable_and_stops_oauth_retries(self):
        client = Mock()
        client.get.side_effect = EbayError('OAuth-Tokenfehler (HTTP 400).')
        data = risk.collect(client, ['order1'])
        self.assertEqual(client.get.call_count, 1)
        client.pages.assert_not_called()
        self.assertTrue(all(not v['available'] for v in data['resources'].values()))

    def test_scope_failure_does_not_hide_other_resources(self):
        client = Mock()
        client.get.side_effect = lambda endpoint, *a: (_ for _ in ()).throw(EbayError('Scope fehlt (HTTP 403)')) if endpoint == 'standards' else {}
        client.pages.return_value = {'items': [], 'pages': []}
        data = risk.collect(client)
        self.assertFalse(data['resources']['standards']['available'])
        self.assertTrue(data['resources']['returns']['available'])

    def financial(self, status='PAYOUT', amount='491.80'):
        return snapshot(reference_payout={'payoutId': risk.PAYOUT, 'amount': {'value': '491.80', 'currency': 'EUR'}}, reference_transactions={'items': [
            {'transactionId': '1', 'transactionType': 'SALE', 'transactionStatus': status, 'bookingEntry': 'CREDIT', 'payoutId': risk.PAYOUT, 'amount': {'value': amount, 'currency': 'EUR'}}]})

    def test_reference_reconstructs_only_explicit_final_rows(self):
        data = self.financial(amount='521.79')
        rows = data['resources']['reference_transactions']['data']['items']
        rows.append({'transactionId': '2', 'transactionType': 'REFUND', 'transactionStatus': 'PAYOUT', 'bookingEntry': 'DEBIT', 'payoutId': risk.PAYOUT, 'amount': {'value': '29.99', 'currency': 'EUR'}})
        rows.append({'transactionId': '3', 'transactionType': 'SALE', 'transactionStatus': 'FUNDS_ON_HOLD', 'bookingEntry': 'CREDIT', 'payoutId': risk.PAYOUT, 'amount': {'value': '1108.47', 'currency': 'EUR'}})
        result = risk.finance_check(data)
        self.assertTrue(result['reconstructed'])
        self.assertEqual(result['final_sum'], '491.80')
        self.assertEqual(sum(t['berücksichtigt'] for t in result['transactions']), 2)
        rows.append(dict(rows[0]))
        self.assertTrue(risk.finance_check(data)['reconstructed'])

    def test_unknown_status_currency_partial_never_confirm(self):
        self.assertFalse(risk.finance_check(self.financial('FUNDS_ON_HOLD'))['reconstructed'])
        self.assertFalse(risk.finance_check(self.financial('UNKNOWN'))['reconstructed'])
        data = self.financial()
        data['resources']['reference_transactions']['data']['items'][0]['amount']['currency'] = 'USD'
        self.assertFalse(risk.finance_check(data)['reconstructed'])
        self.assertFalse(risk.finance_check(None)['reconstructed'])

    def test_booked_hold_can_have_payout_status_and_count_must_match(self):
        data = self.financial(amount='591.80')
        data['resources']['reference_payout']['data']['transactionCount'] = 2
        data['resources']['reference_transactions']['data']['items'].append({
            'transactionId': 'RETRO_HOLD-1', 'transactionType': 'DISPUTE', 'transactionStatus': 'PAYOUT',
            'bookingEntry': 'DEBIT', 'payoutId': risk.PAYOUT, 'orderId': 'order1',
            'amount': {'value': '100.00', 'currency': 'EUR'}})
        report = risk.finance_check(data)
        self.assertTrue(report['reconstructed'])
        self.assertEqual(len(report['booked_hold_movements']), 1)
        self.assertEqual(report['order_holds'], [])
        data['resources']['reference_payout']['data']['transactionCount'] = 3
        self.assertFalse(risk.finance_check(data)['reconstructed'])

    def test_ui_cached_audit_and_missing_secrets_never_call_network(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as folder:
            data = self.financial()
            data['resources']['standards'] = {'available': True, 'data': {'standardsProfiles': [
                {'cycle': {'cycleType': 'CURRENT'}, 'program': 'PROGRAM_DE', 'standardsLevel': 'ABOVE_STANDARD'},
                {'cycle': 'PROJECTED', 'program': 'PROGRAM_DE', 'standardsLevel': 'BELOW_STANDARD'}]}}
            data['resources']['returns'] = {'available': True, 'data': {'items': [{'returnId': 'r1', 'orderId': 'order1', 'state': 'OPEN', 'creationInfo': {'reason': 'DAMAGED'}}]}}
            risk.save_snapshot(folder, data)
            script = '\n'.join([
                'import pandas as pd', 'import trust_risk_ui',
                f'catalogue=pd.DataFrame({self.catalogue.to_dict("records")!r})',
                f'trust_risk_ui.render({folder!r},catalogue,pd.DataFrame(),pd.DataFrame())'])
            with patch('requests.sessions.Session.request', side_effect=AssertionError('No HTTP')), patch('trust_risk_ui.secrets_config', side_effect=EbayError('Zugangsdaten nicht verfügbar')):
                app = AppTest.from_string(script).run(timeout=20)
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(app.button(key='ebay-risk-refresh').disabled)
            self.assertTrue(any('MH:' in block.value for block in app.code))
            self.assertTrue(any(m.value == 'ABOVE_STANDARD' for m in app.metric))

    def test_collector_uses_real_dispute_response_name(self):
        client = Mock()
        client.get.return_value = {}
        client.pages.return_value = {'items': [], 'pages': []}
        risk.collect(client)
        self.assertIn(('disputes', 'paymentDisputeSummaries'), [c.args for c in client.pages.call_args_list])


if __name__ == '__main__':
    unittest.main()
