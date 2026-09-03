import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import core
from test_recovery import payout


class ApiWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = patch.multiple(core, PAYOUTS_DB_PATH=str(Path(self.temp.name)/'Master_Payouts.csv'),
                                    ORDERS_DB_PATH=str(Path(self.temp.name)/'Master_Orders.csv'))
        self.paths.start()
        self.addCleanup(self.paths.stop)
        order = payout(title='VERBINDLICHER BESTELLTITEL', sku='NB / ORDER')
        core.import_reports([order], core.ORDERS_DB_PATH, 'orders')
        source = payout(title='NICHT VERWENDEN', sku='PP / PAYOUT')
        source['Auszahlungsstatus'] = 'Betrag überwiesen'
        core.import_reports([source], core.PAYOUTS_DB_PATH, 'payout')
        self.http = Mock()
        self.http.get.return_value.status_code = 200
        self.http.get.return_value.json.return_value = {'content': [{'id': 'contact', 'roles': {'customer': {'number': 16335}}}]}
        self.http.post.return_value.status_code = 201
        self.http.post.return_value.json.return_value = {'id': 'draft-1'}

    def send(self):
        return core.create_invoice_draft('fake-key', '7700379513', True, self.http)

    def test_authoritative_title_and_sku(self):
        row = core.load_master_data().iloc[0]
        self.assertEqual(row.Angebotstitel, 'VERBINDLICHER BESTELLTITEL')
        self.assertEqual(row.SKU, 'NB / ORDER')
        self.assertEqual(row.Gruppe, 'Gruppe B')
        self.assertEqual(row['Titelquelle'], 'Bestellbericht')

    def test_payout_title_never_bypasses_missing_order(self):
        with patch.object(core, 'ORDERS_DB_PATH', str(Path(self.temp.name)/'missing.csv')):
            master = core.load_master_data()
            self.assertIn('Zuordnung fehlt', master.iloc[0]['Prüfhinweis'])
            with self.assertRaises(ValueError):
                core.build_invoice_payload(master, '7700379513', 'contact', True)

    def test_success_draft_only_restart_duplicate_and_statuses(self):
        core.confirm_received('7700379513')
        self.assertEqual(self.send(), 'draft-1')
        kwargs = self.http.post.call_args.kwargs
        self.assertEqual(kwargs['params'], {'finalize': 'false'})
        self.assertEqual(kwargs['json']['lineItems'][0]['name'], 'VERBINDLICHER BESTELLTITEL')
        with self.assertRaises(ValueError):
            self.send()
        self.assertEqual(self.http.post.call_count, 1)
        for status in core.FOLLOWUP.values():
            core.advance_status('7700379513', status)
        states = core.sync_status(core.load_master_data())
        self.assertEqual(states.iloc[0].Status, 'abgeschlossen')
        with core.ledger() as db:
            self.assertGreater(db.execute('SELECT count(*) FROM audit').fetchone()[0], 4)

    def test_timeout_remains_locked(self):
        core.confirm_received('7700379513')
        self.http.post.side_effect = TimeoutError('secret must not leak')
        with self.assertRaisesRegex(ValueError, 'bleibt gesperrt'):
            self.send()
        with self.assertRaises(ValueError):
            self.send()
        self.assertEqual(self.http.post.call_count, 1)

    def test_crash_after_reservation_remains_locked(self):
        core.confirm_received('7700379513')
        self.http.post.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.send()
        with self.assertRaises(ValueError):
            self.send()

    def test_unknown_response_and_http_error_remain_locked(self):
        core.confirm_received('7700379513')
        self.http.post.return_value.status_code = 500
        with self.assertRaises(ValueError):
            self.send()
        with self.assertRaises(ValueError):
            self.send()

    def test_no_receipt_no_post_and_contact_error_no_post(self):
        with self.assertRaises(ValueError):
            self.send()
        self.http.post.assert_not_called()
        core.confirm_received('7700379513')
        self.http.get.return_value.status_code = 401
        with self.assertRaises(ValueError):
            self.send()
        self.http.post.assert_not_called()

    def test_closed_data_changes_blocked(self):
        core.confirm_received('7700379513')
        self.send()
        altered = core.load_master_data()
        altered.loc[0, 'Angebotstitel'] = 'Manipuliert'
        state = core.sync_status(altered)
        self.assertIn('gesperrte Daten verändert', state.iloc[0].Status)

    def test_parallel_click_only_one_post(self):
        from concurrent.futures import ThreadPoolExecutor
        core.confirm_received('7700379513')
        def attempt():
            try:
                return self.send()
            except ValueError:
                return 'blocked'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(sorted(outcomes), ['blocked', 'draft-1'])
        self.assertEqual(self.http.post.call_count, 1)

    def test_backup_contains_durable_invoice_lock(self):
        import io, zipfile, sqlite3
        core.confirm_received('7700379513')
        self.send()
        with zipfile.ZipFile(io.BytesIO(core.backup_data())) as archive:
            self.assertEqual(set(archive.namelist()), {'Master_Payouts.csv', 'Master_Orders.csv', 'Settlement_State.sqlite3', 'Settlement_Locks.json'})
            target = Path(self.temp.name) / 'backup.sqlite3'
            target.write_bytes(archive.read('Settlement_State.sqlite3'))
        with sqlite3.connect(target) as db:
            self.assertEqual(db.execute('SELECT invoice_id FROM payouts').fetchone()[0], 'draft-1')


if __name__ == '__main__':
    unittest.main()
