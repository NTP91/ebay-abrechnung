"""Read-only eBay Trading API adapter for messages and received feedback."""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from ebay_readonly import EbayError

URL = 'https://api.ebay.com/ws/api.dll'
NS = 'urn:ebay:apis:eBLBaseComponents'


def _text(node, name, default=''):
    child = node.find('.//{' + NS + '}' + name)
    return (child.text or '').strip() if child is not None else default


def _plain(value):
    value = re.sub(r'<[^>]+>', ' ', html.unescape(value or ''))
    return re.sub(r'\s+', ' ', value).strip()


class TradingClient:
    def __init__(self, rest_client):
        self.rest = rest_client

    def call(self, name, body):
        token = self.rest.access_token()
        xml = ('<?xml version="1.0" encoding="utf-8"?>'
               f'<{name}Request xmlns="{NS}"><RequesterCredentials>'
               f'<eBayAuthToken>{html.escape(token)}</eBayAuthToken>'
               f'</RequesterCredentials><ErrorLanguage>de_DE</ErrorLanguage>'
               f'<WarningLevel>High</WarningLevel>{body}</{name}Request>')
        response = self.rest._request('POST', URL, data=xml.encode('utf-8'), headers={
            'Content-Type': 'text/xml', 'X-EBAY-API-CALL-NAME': name,
            'X-EBAY-API-SITEID': '77', 'X-EBAY-API-COMPATIBILITY-LEVEL': '1451'})
        if response.status_code != 200:
            raise EbayError(f'Trading API {name} fehlgeschlagen (HTTP {response.status_code}).')
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            raise EbayError(f'Trading API {name} lieferte ungültiges XML.') from None
        ack = _text(root, 'Ack')
        if ack not in ('Success', 'Warning'):
            code = _text(root, 'ErrorCode') or 'unbekannt'
            message = _text(root, 'LongMessage') or _text(root, 'ShortMessage')
            raise EbayError(f'Trading API {name}: {code} · {message}')
        return root

    def my_messages(self, days=90):
        """Read all available Inbox/Sent/Deleted message headers, then bodies in batches."""
        headers = {}
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        end = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        for folder in ('0', '1', '2'):
            page = 1
            while True:
                root = self.call('GetMyMessages',
                    '<DetailLevel>ReturnHeaders</DetailLevel>'
                    f'<StartTime>{start}</StartTime><EndTime>{end}</EndTime>'
                    f'<FolderID>{folder}</FolderID><Pagination><EntriesPerPage>200</EntriesPerPage>'
                    f'<PageNumber>{page}</PageNumber></Pagination>')
                for node in root.findall('.//{' + NS + '}Message'):
                    mid = _text(node, 'MessageID')
                    if mid:
                        headers[mid] = {'message_id': mid, 'folder_id': folder,
                            'item_id': _text(node, 'ItemID'), 'subject': _text(node, 'Subject'),
                            'received_at': _text(node, 'ReceiveDate'), 'sender': _text(node, 'Sender'),
                            'recipient': _text(node, 'RecipientUserID'),
                            'reply_present': _text(node, 'Replied').lower() == 'true'}
                more = _text(root, 'HasMoreMessages').lower() == 'true'
                if not more:
                    break
                page += 1
                if page > 100:
                    raise EbayError('GetMyMessages Abrufgrenze erreicht.')
        result = []
        ids = list(headers)
        for offset in range(0, len(ids), 10):
            chunk = ids[offset:offset + 10]
            id_xml = ''.join(f'<MessageID>{html.escape(i)}</MessageID>' for i in chunk)
            root = self.call('GetMyMessages', '<DetailLevel>ReturnMessages</DetailLevel>'
                             f'<MessageIDs>{id_xml}</MessageIDs>')
            for node in root.findall('.//{' + NS + '}Message'):
                mid = _text(node, 'MessageID')
                record = dict(headers.get(mid, {'message_id': mid}))
                record.update({
                    'item_id': _text(node, 'ItemID') or record.get('item_id', ''),
                    'subject': _text(node, 'Subject') or record.get('subject', ''),
                    'text': _plain(_text(node, 'Text') or _text(node, 'Content')),
                    'received_at': _text(node, 'ReceiveDate') or record.get('received_at', ''),
                    'sender': _text(node, 'Sender') or record.get('sender', ''),
                    'recipient': _text(node, 'RecipientUserID') or record.get('recipient', ''),
                    'reply_present': _text(node, 'Replied').lower() == 'true' or record.get('reply_present', False),
                    'attachment_present': bool(node.findall('.//{' + NS + '}MessageMedia')),
                    'external_message_id': _text(node, 'ExternalMessageID'),
                })
                result.append(record)
        return result

    def member_messages(self, days=90):
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        end = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        result, page = [], 1
        while True:
            root = self.call('GetMemberMessages',
                f'<StartCreationTime>{start}</StartCreationTime><EndCreationTime>{end}</EndCreationTime>'
                '<MailMessageType>All</MailMessageType>'
                f'<Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>')
            for node in root.findall('.//{' + NS + '}MemberMessageExchange'):
                message = node.find('.//{' + NS + '}Question') or node
                result.append({'message_id': _text(message, 'MessageID'), 'item_id': _text(node, 'ItemID'),
                    'subject': _text(message, 'Subject'), 'text': _plain(_text(message, 'Body')),
                    'received_at': _text(message, 'CreationDate'), 'sender': _text(message, 'SenderID'),
                    'recipient': '', 'reply_present': bool(node.find('.//{' + NS + '}Response')),
                    'attachment_present': bool(node.findall('.//{' + NS + '}MessageMedia')),
                    'external_message_id': _text(message, 'ExternalMessageID')})
            if _text(root, 'HasMoreItems').lower() != 'true':
                break
            page += 1
            if page > 100:
                raise EbayError('GetMemberMessages Abrufgrenze erreicht.')
        return result


    def negative_feedback(self):
        result, page = [], 1
        while True:
            root = self.call('GetFeedback',
                '<DetailLevel>ReturnAll</DetailLevel><FeedbackType>FeedbackReceivedAsSeller</FeedbackType>'
                '<CommentType>Negative</CommentType><CommentType>Neutral</CommentType>'
                f'<Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>')
            for node in root.findall('.//{' + NS + '}FeedbackDetail'):
                result.append({'feedback_id': _text(node, 'FeedbackID') or (_text(node, 'OrderLineItemID') + ':' + _text(node, 'CommentTime')),
                    'order_line_item_id': _text(node, 'OrderLineItemID'), 'item_id': _text(node, 'ItemID'),
                    'comment_type': _text(node, 'CommentType'), 'comment_text': _plain(_text(node, 'CommentText')),
                    'event_at': _text(node, 'CommentTime'), 'buyer': _text(node, 'CommentingUser')})
            pages = _text(root, 'TotalNumberOfPages')
            if not pages or page >= int(pages):
                break
            page += 1
        return result


def merge_messages(my_messages, member_messages):
    """Prefer mailbox records and add only semantically new member messages."""
    result=[]; ids=set(); semantic=set()
    for origin, rows in (('GetMyMessages',my_messages),('GetMemberMessages',member_messages)):
        for row in rows:
            current=dict(row); current['message_api']=origin
            if origin=='GetMemberMessages': current['sender_role']='buyer'
            identifier=str(current.get('message_id') or '').strip()
            signature=(str(current.get('sender') or '').casefold(),str(current.get('item_id') or ''),
                       str(current.get('received_at') or '')[:16],_plain(current.get('text') or '').casefold())
            if (identifier and identifier in ids) or signature in semantic:
                continue
            if identifier: ids.add(identifier)
            semantic.add(signature); result.append(current)
    return result
