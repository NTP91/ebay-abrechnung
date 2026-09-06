"""Allow-listed read-only eBay Trading API transport for audit evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import xml.etree.ElementTree as ET

from ebay_readonly import EbayError


URL = "https://api.ebay.com/ws/api.dll"
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
ALLOWED = {"GetMyMessages", "GetMemberMessages", "GetFeedback"}


def _tag(element, name, default=""):
    value = element.findtext("e:" + name, default=default, namespaces=NS)
    return (value or "").strip()


def _xml(name, body):
    if name not in ALLOWED:
        raise EbayError("Nicht freigegebener Trading-Leseaufruf.")
    return (f'<?xml version="1.0" encoding="utf-8"?>'
            f'<{name}Request xmlns="urn:ebay:apis:eBLBaseComponents">{body}</{name}Request>')


class TradingClient:
    """Trading calls use POST as required by eBay, but only read operations are allowed."""

    def __init__(self, oauth_client, compatibility="1423"):
        self.oauth = oauth_client
        self.compatibility = compatibility

    def call(self, name, body):
        token = self.oauth.access_token()
        headers = {
            "X-EBAY-API-CALL-NAME": name,
            "X-EBAY-API-SITEID": "77",
            "X-EBAY-API-COMPATIBILITY-LEVEL": self.compatibility,
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        }
        response = self.oauth._request("POST", URL, headers=headers, data=_xml(name, body).encode("utf-8"))
        if not 200 <= response.status_code < 300:
            raise EbayError(f"Trading API-Fehler (HTTP {response.status_code}).")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            raise EbayError("Trading API-Antwort enthält kein gültiges XML.") from None
        ack = _tag(root, "Ack")
        if ack not in ("Success", "Warning"):
            codes = sorted({_tag(error, "ErrorCode") for error in root.findall("e:Errors", NS) if _tag(error, "ErrorCode")})
            suffix = " (eBay " + ", ".join(codes) + ")" if codes else ""
            raise EbayError("Trading API-Leseaufruf fehlgeschlagen" + suffix + ".")
        return root

    @staticmethod
    def _period(start, end):
        def stamp(value):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return f"<StartTime>{stamp(start)}</StartTime><EndTime>{stamp(end)}</EndTime>"

    def _paged(self, name, body, path, mapper, page_size=200, max_pages=100):
        result = []
        for page in range(1, max_pages + 1):
            root = self.call(name, body + f"<Pagination><EntriesPerPage>{page_size}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>")
            rows = root.findall(path, NS)
            result.extend(mapper(row) for row in rows)
            total_pages = int(root.findtext(".//e:PaginationResult/e:TotalNumberOfPages", "1", NS) or 1)
            if page >= total_pages:
                return result
        raise EbayError("Trading API-Abrufgrenze erreicht; Daten sind nicht vollständig.")

    def member_message_headers(self, start, end):
        body = "<MailMessageType>All</MailMessageType><MessageStatus>Unanswered</MessageStatus>" + self._period(start, end)
        return self._paged("GetMemberMessages", body, ".//e:MemberMessageExchange", self._member_message)

    def my_message_headers(self, start, end):
        result = {}
        for folder in (0, 1, 2):  # Inbox, Sent, Deleted/archived where retained by eBay.
            body = f"<DetailLevel>ReturnHeaders</DetailLevel><FolderID>{folder}</FolderID>" + self._period(start, end)
            for row in self._paged("GetMyMessages", body, ".//e:Message", self._message):
                row["folder_id"] = str(folder)
                row["sender_role"] = "buyer" if folder == 0 else "seller" if folder == 1 else "unknown"
                result[row["external_id"]] = row
        return list(result.values())

    def my_messages(self, message_ids):
        result = []
        unique = list(dict.fromkeys(str(value) for value in message_ids if value))
        for offset in range(0, len(unique), 10):
            identifiers = "".join(f"<MessageID>{value}</MessageID>" for value in unique[offset:offset + 10])
            root = self.call("GetMyMessages", f"<DetailLevel>ReturnMessages</DetailLevel><MessageIDs>{identifiers}</MessageIDs>")
            result.extend(self._message(row) for row in root.findall(".//e:Message", NS))
        return result

    def negative_feedback(self):
        body = "<FeedbackType>FeedbackReceivedAsSeller</FeedbackType><CommentType>Negative</CommentType><DetailLevel>ReturnAll</DetailLevel>"
        return self._paged("GetFeedback", body, ".//e:FeedbackDetail", self._feedback)

    def message_history(self, start, end):
        """Probe both APIs and select the broadest history with line-item references."""
        mine = self.my_message_headers(start, end)
        member = self.member_message_headers(start, end)
        mine_refs = sum(bool(row.get("line_item_id") or row.get("transaction_id")) for row in mine)
        member_refs = sum(bool(row.get("line_item_id") or row.get("transaction_id")) for row in member)
        selected = "GetMyMessages" if (mine_refs, len(mine)) >= (member_refs, len(member)) else "GetMemberMessages"
        if selected == "GetMyMessages":
            headers = {row["external_id"]: row for row in mine}
            rows = []
            for detail in self.my_messages(headers):
                header = headers.get(detail["external_id"], {})
                merged = {**header, **detail}
                if detail.get("sender_role") == "unknown":
                    merged["sender_role"] = header.get("sender_role", "unknown")
                if not detail.get("folder_id"):
                    merged["folder_id"] = header.get("folder_id", "")
                rows.append(merged)
        else:
            rows = member
        return {"selected": selected, "rows": rows,
                "coverage": {"GetMyMessages": {"count": len(mine), "line_references": mine_refs},
                             "GetMemberMessages": {"count": len(member), "line_references": member_refs}}}

    @staticmethod
    def _message(row):
        folder = _tag(row, "FolderID")
        replied = _tag(row, "Replied").casefold() == "true"
        response_text = row.findtext("e:ResponseDetails/e:Content", default="", namespaces=NS) or ""
        all_tags = {node.tag.rsplit("}", 1)[-1].casefold() for node in row.iter()}
        text = _tag(row, "Text")
        return {"external_id": _tag(row, "MessageID") or _tag(row, "ExternalMessageID"),
                "order_id": _tag(row, "OrderID"), "line_item_id": _tag(row, "OrderLineItemID"),
                "transaction_id": _tag(row, "TransactionID"), "item_id": _tag(row, "ItemID"),
                "subject": _tag(row, "Subject"), "text": text,
                "event_at": _tag(row, "ReceiveDate") or _tag(row, "CreationDate"),
                "status": _tag(row, "Read"), "sender": _tag(row, "Sender"),
                "recipient": _tag(row, "RecipientUserID") or _tag(row, "SendToName"),
                "sender_role": "buyer" if folder == "0" else "seller" if folder == "1" else "unknown",
                "reply_present": replied or bool(response_text.strip()),
                "response_text": response_text.strip(), "folder_id": folder,
                "attachment_present": bool(all_tags & {"messagemedia", "mediaurl", "attachment", "image"})
                    or any(word in text.casefold() for word in ("anhang", "foto", "bild", "photo"))}

    @staticmethod
    def _member_message(row):
        question = row.find("e:Question", NS) or row
        return {"external_id": _tag(question, "MessageID"), "order_id": "", "line_item_id": "",
                "transaction_id": _tag(question, "TransactionID"), "item_id": _tag(question, "ItemID"),
                "subject": _tag(question, "Subject"), "text": _tag(question, "Body"),
                "event_at": _tag(question, "CreationDate"), "status": _tag(question, "MessageStatus")}

    @staticmethod
    def _feedback(row):
        return {"external_id": _tag(row, "FeedbackID") or ":".join((_tag(row, "ItemID"), _tag(row, "TransactionID"), _tag(row, "CommentTime"))),
                "order_id": "", "line_item_id": _tag(row, "OrderLineItemID"),
                "transaction_id": _tag(row, "TransactionID"), "item_id": _tag(row, "ItemID"),
                "text": _tag(row, "CommentText"), "event_at": _tag(row, "CommentTime"),
                "status": _tag(row, "CommentType")}
