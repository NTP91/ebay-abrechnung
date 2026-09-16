import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import core
import data_status
from test_recovery import Upload, payout


class OrderImportCoverageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        paths = patch.multiple(
            core,
            PAYOUTS_DB_PATH=str(self.root / 'Master_Payouts.csv'),
            ORDERS_DB_PATH=str(self.root / 'Master_Orders.csv'),
        )
        paths.start()
        self.addCleanup(paths.stop)

    def report(self, rows, name='orders.csv'):
        frames = []
        for transaction, order, sold_at in rows:
            frame = payout(transaction=transaction, order=order)
            frame['Verkauft am'] = sold_at
            frames.append(frame)
        return Upload(pd.concat(frames).to_csv(sep=';', index=False).encode('utf-8'), name)

    def imports(self):
        with core.ledger() as db:
            return pd.read_sql_query('SELECT * FROM imports ORDER BY id', db)

    def test_overlapping_successful_reports_merge_coverage_without_duplicates(self):
        first = data_status.import_file(
            self.report([('t1', 'o1', '02.09.2026')], 'first.csv'),
            'orders', '2026-09-01', '2026-09-03')
        second = data_status.import_file(
            self.report([('t1', 'o1', '02.09.2026'), ('t2', 'o2', '04.09.2026')], 'second.csv'),
            'orders', '2026-09-02', '2026-09-05')

        self.assertEqual((first['status'], second['status']), ('success', 'success'))
        self.assertEqual(len(core.read_master(core.ORDERS_DB_PATH)), 2)
        imports = self.imports()
        self.assertTrue(imports['at'].notna().all())
        self.assertEqual(imports.status.tolist(), ['success', 'success'])
        self.assertEqual(imports.coverage_start.tolist(), ['2026-09-01', '2026-09-02'])
        state = data_status.coverage(imports)
        self.assertEqual(state['intervals'], [[pd.Timestamp('2026-09-01').date(), pd.Timestamp('2026-09-05').date()]])
        self.assertEqual(state['gaps'], [])

    def test_uncovered_calendar_days_are_reported_exactly(self):
        data_status.import_file(self.report([('t1', 'o1', '01.09.2026')]), 'orders', '2026-09-01', '2026-09-02')
        data_status.import_file(self.report([('t2', 'o2', '05.09.2026')]), 'orders', '2026-09-05', '2026-09-06')

        state = data_status.coverage(self.imports())
        self.assertEqual(state['gaps'], [(pd.Timestamp('2026-09-03').date(), pd.Timestamp('2026-09-04').date())])

    def test_failed_import_does_not_advance_success_or_coverage(self):
        good = data_status.import_file(self.report([('t1', 'o1', '01.09.2026')]), 'orders', '2026-09-01', '2026-09-02')
        before = Path(core.ORDERS_DB_PATH).read_bytes()
        bad = data_status.import_file(Upload(b'not a report', 'broken.csv'), 'orders', '2026-09-03', '2026-09-04')

        self.assertEqual(good['status'], 'success')
        self.assertEqual(bad['status'], 'failed')
        self.assertEqual(before, Path(core.ORDERS_DB_PATH).read_bytes())
        state = data_status.coverage(self.imports())
        self.assertEqual((state['start'].isoformat(), state['end'].isoformat()), ('2026-09-01', '2026-09-02'))
        self.assertEqual(state['last_success'].filename, 'orders.csv')
        self.assertEqual(state['last_attempt'].filename, 'broken.csv')

    def test_missing_or_false_coverage_fails_before_data_write(self):
        upload = self.report([('t1', 'o1', '05.09.2026')])
        missing = data_status.import_file(upload, 'orders')
        outside = data_status.import_file(upload, 'orders', '2026-09-01', '2026-09-04')

        self.assertEqual((missing['status'], outside['status']), ('failed', 'failed'))
        self.assertIn('Berichtszeitraum fehlt', missing['error'])
        self.assertIn('außerhalb', outside['error'])
        self.assertFalse(Path(core.ORDERS_DB_PATH).exists())
        self.assertIsNone(data_status.coverage(self.imports())['last_success'])


if __name__ == '__main__':
    unittest.main()
