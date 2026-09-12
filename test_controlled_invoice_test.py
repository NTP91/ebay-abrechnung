import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import core
import controlled_invoice_test as target


class SingleAttemptTests(unittest.TestCase):
    def exercise(self, timeout):
        with tempfile.TemporaryDirectory() as folder, patch.object(core, 'PAYOUTS_DB_PATH', str(Path(folder) / 'Master_Payouts.csv')), patch.object(core, 'ORDERS_DB_PATH', str(Path(folder) / 'Master_Orders.csv')):
            with core.ledger() as db:
                db.executemany('INSERT INTO payouts(id,status) VALUES(?,?)', [(p, 'vollständig zugeordnet') for p in target.PAYOUTS])
                db.commit()
            master = pd.DataFrame({'Auszahlung Nr.': target.PAYOUTS})
            http = Mock()
            http.get.return_value.status_code = 200
            http.get.return_value.json.return_value = {'roles': {'customer': {'number': 16335}}}

            def post(*args, **kwargs):
                # A separate connection observes the committed reservation BEFORE POST.
                with core.ledger() as db:
                    self.assertEqual([r['attempt'] for r in db.execute('SELECT * FROM payouts')], ['pending'] * 3)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM audit').fetchone()[0], 3)
                self.assertEqual(kwargs['params'], {'finalize': 'false'})
                self.assertFalse(kwargs['allow_redirects'])
                if timeout:
                    raise TimeoutError()
                response = Mock(status_code=201)
                response.json.return_value = {'id': 'new-draft'}
                return response

            http.post.side_effect = post
            with patch.object(target, 'prepare', return_value=(master, {'lineItems': []}, 'hash')), patch.object(core, 'payout_fingerprint', return_value='fingerprint'):
                if timeout:
                    with self.assertRaises(RuntimeError):
                        target.create_once('fake', Path('fake.xlsx'), http)
                else:
                    self.assertEqual(target.create_once('fake', Path('fake.xlsx'), http)[0], 'new-draft')
                with self.assertRaises(ValueError):
                    target.create_once('fake', Path('fake.xlsx'), http)
            self.assertEqual(http.post.call_count, 1)
            with core.ledger() as db:
                rows = db.execute('SELECT * FROM payouts').fetchall()
                self.assertTrue(all(r['attempt'] == ('unknown' if timeout else 'created') for r in rows))
                self.assertTrue(all(r['invoice_id'] == (None if timeout else 'new-draft') for r in rows))

    def test_success_blocks_second_post(self):
        self.exercise(False)

    def test_timeout_blocks_second_post(self):
        self.exercise(True)


if __name__ == '__main__':
    unittest.main()
