import io
import unittest

import pdfplumber

import generate_mh_audit_20260908 as audit


class UpdatedMhAuditTests(unittest.TestCase):
    def test_case_register_is_deduplicated_and_contains_the_three_new_cases(self):
        orders = [row["order"] for row in audit.ALL_CASES]
        self.assertEqual(30, len(orders))
        self.assertEqual(len(orders), len(set(orders)))
        self.assertTrue({"03-15116-91038", "14-15092-67058", "18-15105-36957"}.issubset(orders))

    def test_new_cases_keep_their_evidence_based_categories(self):
        cases = {row["order"]: row for row in audit.ALL_CASES}
        self.assertIn("INAD", cases["03-15116-91038"]["problem"])
        self.assertIn("Defekt", cases["14-15092-67058"]["problem"])
        self.assertIn("Variante", cases["18-15105-36957"]["problem"])
        self.assertIn("Hold", cases["18-15105-36957"]["beleg"])

    def test_pdf_contains_update_date_comparison_and_evidence_orders(self):
        with pdfplumber.open(io.BytesIO(audit.generate_pdf_bytes())) as reader:
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            self.assertGreaterEqual(len(reader.pages), 12)
        for value in ("Datenstand: 08.09.2026", "Vergleich 06.09.2026 vs. 08.09.2026",
                      "03-15116-91038", "14-15092-67058", "18-15105-36957"):
            self.assertIn(value, text)


if __name__ == "__main__":
    unittest.main()
