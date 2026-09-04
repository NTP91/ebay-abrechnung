import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import core
import data_status
from payout_structure import validate
from test_recovery import payout, Upload


class ParentChildTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(self.root/'Master_Payouts.csv'), ORDERS_DB_PATH=str(self.root/'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('No HTTP'))
        network.start(); self.addCleanup(network.stop)
        parent = payout('7718008497', transaction='', order='18-15098-91741', amount='149.96')
        parent['Transaktionsbetrag (inkl. Kosten)'] = '149.96'
        rows = [parent]
        for transaction, item, amount in [('10087571312118','i1','99.98'),('10087571312218','i2','49.98')]:
            child = payout('7718008497',transaction=transaction,order='18-15098-91741',amount='--')
            child['Artikelnummer'] = item
            child['Zwischensumme Artikel'] = amount
            child['Verpackung und Versand'] = '0'
            child['Transaktionsbetrag (inkl. Kosten)'] = '--'
            rows.append(child)
        self.frame = core.canonicalize(core.pd.concat(rows,ignore_index=True).fillna(''))
        core.import_reports([self.frame.iloc[1:]],core.ORDERS_DB_PATH,'orders')

    def upload(self,frame):
        return data_status.import_file(Upload(frame.to_csv(sep=';',index=False).encode()),'payout')

    def test_parent_only_finance_and_child_identities_repeat(self):
        result = self.upload(self.frame)
        self.assertFalse(result['error'])
        self.assertFalse(result['transactions']['warnings'])
        raw = core.read_master(core.PAYOUTS_DB_PATH)
        self.assertEqual(len(raw),3)
        self.assertEqual(len(validate(raw)),2)
        self.assertTrue((raw.iloc[1:]['Betrag abzügl. Kosten']=='').all())
        master = core.load_master_data()
        self.assertEqual(len(master),1)
        self.assertEqual(master.Erlös_Brutto.sum(),149.96)
        orders = core.read_master(core.ORDERS_DB_PATH)
        for _,row in raw.iloc[1:].iterrows():
            match,issue=core.match_order(row,orders)
            self.assertFalse(issue)
            self.assertEqual(match.Transaktionsnummer,row.Transaktionsnummer)
            self.assertTrue(match.SKU)
        before=Path(core.PAYOUTS_DB_PATH).read_bytes()
        again=self.upload(self.frame)
        self.assertFalse(again['transactions']['warnings'])
        self.assertEqual(again['added'],0)
        self.assertEqual(Path(core.PAYOUTS_DB_PATH).read_bytes(),before)
        partial=self.upload(self.frame.iloc[1:2])
        self.assertFalse(partial['transactions']['warnings'])
        self.assertEqual(Path(core.PAYOUTS_DB_PATH).read_bytes(),before)

    def test_wrong_sum_rejected(self):
        self.frame.loc[1,'Zwischensumme Artikel']='100.00'
        with self.assertRaisesRegex(ValueError,'Child-Zwischensummen'):validate(self.frame)
        result=self.upload(self.frame)
        self.assertTrue(result['transactions']['warnings'])
        self.assertTrue(core.read_master(core.PAYOUTS_DB_PATH).empty)

    def test_missing_parent_or_single_amount_is_hard_error(self):
        for frame in [self.frame.iloc[1:],payout(amount='--')]:
            with self.assertRaises(ValueError):validate(frame)
            result=self.upload(frame)
            self.assertTrue(result['transactions']['warnings'])
            self.assertEqual(result['added'],0)

    def test_changed_child_and_lock_rejected(self):
        self.upload(self.frame)
        before=Path(core.PAYOUTS_DB_PATH).read_bytes()
        changed=self.frame.copy(); changed.loc[1,'Zwischensumme Artikel']='100.00'
        self.assertTrue(self.upload(changed)['transactions']['warnings'])
        self.assertEqual(Path(core.PAYOUTS_DB_PATH).read_bytes(),before)
        with core.ledger() as db:
            db.execute("UPDATE payouts SET attempt='created',invoice_id='existing'")
            db.commit()
        self.assertFalse(self.upload(self.frame)['transactions']['warnings'])
        extra=self.frame.iloc[1:2].copy();extra['Transaktionsnummer']='new-child'
        self.assertTrue(self.upload(extra)['transactions']['warnings'])
        self.assertEqual(Path(core.PAYOUTS_DB_PATH).read_bytes(),before)
