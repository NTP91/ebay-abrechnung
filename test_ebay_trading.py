import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from ebay_readonly import EbayError
from ebay_trading import TradingClient


def xml(body):
    response = Mock(status_code=200)
    response.content = (f'<GetMyMessagesResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack>{body}</GetMyMessagesResponse>').encode()
    return response


class TradingTests(unittest.TestCase):
    def client(self, responses):
        oauth = Mock()
        oauth.access_token.return_value = "secret"
        oauth._request.side_effect = responses
        return TradingClient(oauth), oauth

    def test_only_allowlisted_read_calls(self):
        client, _ = self.client([])
        with self.assertRaisesRegex(EbayError, "Nicht freigegebener"):
            client.call("AddMemberMessage", "")

    def test_headers_are_mapped_without_token_in_result(self):
        content = xml('<Messages><Message><MessageID>m1</MessageID><OrderLineItemID>item-line1</OrderLineItemID></Message></Messages><PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult>')
        empty = xml('<Messages></Messages><PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult>')
        client, oauth = self.client([content, empty, empty])
        rows = client.my_message_headers(datetime.now(timezone.utc), datetime.now(timezone.utc))
        self.assertEqual(rows[0]["line_item_id"], "item-line1")
        self.assertNotIn("secret", str(rows))
        self.assertEqual(oauth._request.call_args.args[0], "POST")

    def test_message_metadata_preserves_reply_role_and_attachment_hint(self):
        import xml.etree.ElementTree as ET
        row = ET.fromstring('<Message xmlns="urn:ebay:apis:eBLBaseComponents"><MessageID>m</MessageID><FolderID>0</FolderID><Sender>buyer</Sender><RecipientUserID>seller</RecipientUserID><Replied>true</Replied><Text>Foto im Anhang</Text></Message>')
        data = TradingClient._message(row)
        self.assertEqual(data['sender_role'], 'buyer')
        self.assertTrue(data['reply_present'])
        self.assertTrue(data['attachment_present'])

    def test_failed_ack_exposes_only_error_code(self):
        response = Mock(status_code=200)
        response.content = b'<x xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Failure</Ack><Errors><ErrorCode>21917053</ErrorCode><LongMessage>sensitive</LongMessage></Errors></x>'
        client, _ = self.client([response])
        with self.assertRaisesRegex(EbayError, "21917053") as error:
            client.call("GetFeedback", "")
        self.assertNotIn("sensitive", str(error.exception))


if __name__ == "__main__":
    unittest.main()
