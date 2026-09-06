import unittest

from trust_risk_cases import OrderIndex, category_flags, normalize_events


ORDERS = [
    {"bestellnummer": "basket", "transaktionsnummer": "line-1", "artikelnummer": "item-a", "sku": "NB / A", "angebotstitel": "A"},
    {"bestellnummer": "basket", "transaktionsnummer": "line-2", "artikelnummer": "item-b", "sku": "MH43 / B", "angebotstitel": "B"},
    {"bestellnummer": "single", "transaktionsnummer": "line-3", "artikelnummer": "item-c", "sku": "BA / C", "angebotstitel": "C"},
]


class CaseEngineTests(unittest.TestCase):
    def test_identity_keeps_basket_lines_and_normalizes_mh(self):
        index = OrderIndex(ORDERS)
        self.assertEqual(index.resolve({"order_id": "basket", "line_item_id": "line-1"})["partner_id"], "NB")
        self.assertEqual(index.resolve({"order_id": "basket", "line_item_id": "line-2"})["partner_id"], "MH")
        self.assertIsNone(index.resolve({"order_id": "basket"}))

    def test_multiple_signals_count_as_one_case(self):
        signals = [
            ("return", {"external_id": "r1", "order_id": "single", "line_item_id": "line-3", "reason": "DEFECTIVE", "status": "OPEN"}),
            ("message", {"external_id": "m1", "line_item_id": "item-c-line-3", "text": "Artikel defekt", "status": "READ"}),
        ]
        result = normalize_events(ORDERS, signals)
        self.assertEqual(len(result["cases"]), 1)
        self.assertEqual(len(result["signals"]), 2)
        self.assertTrue(result["cases"][0]["has_return"])
        self.assertTrue(result["cases"][0]["has_message"])
        self.assertTrue(result["cases"][0]["defective"])

    def test_ambiguous_signal_is_never_guessed(self):
        result = normalize_events(ORDERS, [("message", {"external_id": "m1", "order_id": "basket", "text": "falsch"})])
        self.assertEqual(result["cases"], [])
        self.assertEqual(len(result["unmatched"]), 1)

    def test_duplicate_unmatched_signal_is_stored_once(self):
        signal = {"external_id": "m1", "order_id": "basket", "text": "falsch"}
        result = normalize_events(ORDERS, [("message", signal), ("message", dict(signal))])
        self.assertEqual(len(result["unmatched"]), 1)

    def test_problem_categories_are_independent_flags(self):
        flags = category_flags("Falscher Artikel, Teile fehlen und bereits geöffnet")
        self.assertTrue(flags["wrong_item"])
        self.assertTrue(flags["incomplete_parts"])
        self.assertTrue(flags["opened_used"])
        self.assertFalse(flags["other_complaint"])
        self.assertTrue(category_flags("NOT_AS_DESCRIBED")["not_as_described"])
        self.assertTrue(category_flags("DEFECTIVE_ITEM")["defective"])
        self.assertFalse(category_flags("Vielen Dank für die schnelle Antwort")["other_complaint"])

    def test_hold_does_not_become_other_complaint(self):
        result = normalize_events(ORDERS, [("hold", {"external_id": "h1", "line_item_id": "line-3",
                                                      "reason": "FUNDS_ON_HOLD", "status": "FUNDS_ON_HOLD"})])
        self.assertTrue(result["cases"][0]["has_hold"])
        self.assertFalse(result["cases"][0]["other_complaint"])

    def test_duplicate_live_and_historical_hold_is_one_signal(self):
        hold = {"external_id": "tx:line-3", "line_item_id": "line-3", "status": "FUNDS_ON_HOLD"}
        result = normalize_events(ORDERS, [("hold", hold), ("hold", dict(hold))])
        self.assertEqual(len(result["signals"]), 1)

    def test_benign_message_is_not_a_problem_and_seller_reply_is_recorded(self):
        result = normalize_events(ORDERS, [("message", {"external_id": "m1", "line_item_id": "line-3",
            "text": "Vielen Dank", "sender_role": "seller", "reply_present": True})])
        self.assertFalse(result["cases"][0]["is_problem"])
        self.assertEqual(result["cases"][0]["case_status"], "geschlossen")
        self.assertTrue(result["signals"][0]["reply_present"])


class MigrationTests(unittest.TestCase):
    def test_case_schema_has_strict_identity_views_and_rls(self):
        from pathlib import Path
        sql = Path("supabase/migrations/20260906090009_audit_cases.sql").read_text(encoding="utf-8").lower()
        self.assertIn("primary key (order_id, line_item_id, sku, partner_id)", sql)
        self.assertIn("audit_summary_by_partner", sql)
        self.assertIn("audit_summary_by_sku", sql)
        self.assertEqual(sql.count("enable row level security"), 3)
        self.assertIn("security_invoker=true", sql)


if __name__ == "__main__":
    unittest.main()
