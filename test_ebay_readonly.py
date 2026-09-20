import unittest
from unittest.mock import Mock

from ebay_readonly import Client, EbayError
from test_trust_risk import config, response


class CheckResponseTests(unittest.TestCase):
    def make(self, replies):
        session = Mock()
        session.request.side_effect = replies
        clock = Mock(return_value=1000)
        return Client(config, session, clock), session

    def token(self):
        return response({'access_token': 'fake-access', 'expires_in': 3600})

    def test_204_no_content_is_a_successful_empty_payout_page(self):
        # response.json() must never be called for a 204 -- prove it by making it explode.
        empty = response({}, 204)
        empty.json.side_effect = AssertionError('json() must not be called for HTTP 204')
        client, session = self.make([self.token(), empty])
        result = client.pages('payouts', 'payouts', {'filter': 'payoutDate:[2026-09-13..2026-09-20]'})
        self.assertEqual(result, {'items': [], 'pages': [{'total': 0}]})

    def test_normal_200_json_payout_page_is_unchanged(self):
        page = response({'payouts': [{'payoutId': 'p1'}], 'total': 1}, 200)
        client, _ = self.make([self.token(), page])
        result = client.pages('payouts', 'payouts')
        self.assertEqual(result['items'], [{'payoutId': 'p1'}])

    def test_real_api_errors_stay_errors(self):
        # 401 triggers one token-refresh retry before giving up; others fail on the first attempt.
        client, _ = self.make([self.token(), response({}, 401), self.token(), response({}, 401)])
        with self.assertRaisesRegex(EbayError, '401'):
            client.get('funds')
        for status in (403, 429, 500):
            client, _ = self.make([self.token(), response({}, status)])
            with self.assertRaisesRegex(EbayError, str(status)):
                client.get('funds')

    def test_2xx_with_invalid_nonempty_body_stays_an_error(self):
        broken = response({}, 200)
        broken.json.side_effect = ValueError('not valid json')
        client, _ = self.make([self.token(), broken])
        with self.assertRaisesRegex(EbayError, 'kein gültiges JSON-Objekt'):
            client.get('funds')

    def test_2xx_json_array_instead_of_object_stays_an_error(self):
        not_a_dict = response([1, 2, 3], 200)
        client, _ = self.make([self.token(), not_a_dict])
        with self.assertRaisesRegex(EbayError, 'kein gültiges JSON-Objekt'):
            client.get('funds')


if __name__ == '__main__':
    unittest.main()
