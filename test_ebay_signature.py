import base64
import unittest
from unittest.mock import Mock

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ebay_signature import signing_headers
from ebay_readonly import Client, EbayError
from test_trust_risk import config, response


class SignatureTests(unittest.TestCase):
    def test_rfc_base_signature_verifies_and_tampering_fails(self):
        key = Ed25519PrivateKey.generate()
        cfg = {'signing_private_key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
               'signing_jwe': 'a.b.c.d.e', 'signing_expiration': 2000}
        headers = signing_headers('https://apiz.ebay.com/sell/finances/v1/transaction?offset=100', cfg, 1000)
        params = '("x-ebay-signature-key" "@method" "@path" "@authority");created=1000'
        expected = '\n'.join(['"x-ebay-signature-key": a.b.c.d.e', '"@method": GET',
                              '"@path": /sell/finances/v1/transaction', '"@authority": apiz.ebay.com',
                              '"@signature-params": ' + params]).encode()
        signature = base64.b64decode(headers['Signature'][6:-1])
        key.public_key().verify(signature, expected)
        with self.assertRaises(InvalidSignature):
            key.public_key().verify(signature, expected.replace(b'GET', b'POST'))
        self.assertEqual(headers['Signature-Input'], 'sig1=' + params)
        self.assertNotIn('Content-Digest', headers)

    def test_missing_expired_bad_keys_and_other_hosts_fail_closed(self):
        for cfg in ({}, {**config(), 'signing_expiration': 999}, {**config(), 'signing_private_key': 'invalid'}):
            with self.assertRaises(ValueError) as exc:
                signing_headers('https://apiz.ebay.com/sell/finances/v1/payout/1', cfg, 1000)
            self.assertNotIn(cfg.get('signing_private_key', 'NOT_PRESENT'), str(exc.exception))
        with self.assertRaises(ValueError):
            signing_headers('https://other.example/sell/finances/v1/payout/1', config(), 1000)

    def test_central_client_signs_each_finance_page_only(self):
        session = Mock()
        session.request.side_effect = [response({'access_token': 'test', 'expires_in': 3600}),
                                      response({'transactions': [{'transactionId': '1', 'transactionType': 'SALE'}], 'total': 2}),
                                      response({'transactions': [{'transactionId': '2', 'transactionType': 'SALE'}], 'total': 2}), response({})]
        client = Client(config, session, lambda:1000)
        client.pages('transactions', 'transactions')
        client.get('standards')
        calls = session.request.call_args_list
        self.assertTrue(all('Signature' in c.kwargs['headers'] for c in calls[1:3]))
        self.assertNotIn('Signature', calls[3].kwargs['headers'])
        self.assertEqual([c.args[0] for c in calls], ['POST', 'GET', 'GET', 'GET'])

    def test_no_unsigned_fallback(self):
        session = Mock()
        session.request.return_value = response({'access_token': 'test', 'expires_in': 3600})
        cfg = config();cfg.pop('signing_private_key')
        client = Client(lambda:cfg, session, lambda:1000)
        with self.assertRaises(EbayError):
            client.get('funds')
        self.assertEqual(session.request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
