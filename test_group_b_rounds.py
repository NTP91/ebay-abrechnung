import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import core
import group_b_rounds as rounds
import position_workflow
import studio_view
from test_recovery import payout


class GroupBRoundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(root/'Master_Payouts.csv'),
                               ORDERS_DB_PATH=str(root/'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP forbidden'))
        network.start(); self.addCleanup(network.stop)

        frames = [
            payout('p1','sale-r1-mh','r1-mh',sku='MH / A',amount='100,00'),
            payout('p1','sale-r1-nb','r1-nb',sku='NB / A',amount='200,00'),
            payout('p2','sale-r2','r2-mh',sku='MH / B',amount='50,00'),
            payout('p2','sale-zero','r2-zero',sku='MH / C',amount='30,00'),
            payout('p2','refund-zero','r2-zero',sku='MH / C',amount='-30,00',kind='Rückerstattung'),
            payout('p2','refund-late','r1-nb',sku='NB / A',amount='-10,00',kind='Rückerstattung'),
        ]
        for frame in frames:
            frame['Artikelnummer']='item-'+frame.iloc[0]['Bestellnummer']
            frame['Transaktionsbetrag (inkl. Kosten)']=frame['Betrag abzügl. Kosten']
            frame['Auszahlungsdatum']='08.09.2026'; frame['Auszahlungsstatus']='Betrag überwiesen'
        core.import_reports(frames,core.ORDERS_DB_PATH,'orders')
        core.import_reports(frames,core.PAYOUTS_DB_PATH,'payout')
        master=core.load_master_data(); core.sync_status(master)
        payload=core.build_invoice_payload(master,'p1','contact',True)
        self.invoice_id='invoice-re0090'
        with core.ledger() as db:
            db.execute("UPDATE payouts SET invoice_id=?,attempt='created',snapshot=? WHERE id='p1'",
                       (self.invoice_id,json.dumps(payload)))
            db.commit()
        self.business=position_workflow.positions()
        self.current=self.business[(self.business.Bestellnummer=='r2-mh')&(self.business.Art=='Bestellung')]
        self.amount=studio_view._snapshot_total(payload)
        self.invoices={self.invoice_id:{'Belegnummer':'RE0090','Betrag':self.amount,
                                       'Payouts':['p1'],'discarded':False}}

    def test_bootstrap_is_immutable_and_maps_refunds_once(self):
        rounds.bootstrap(self.business,self.current,self.invoices)
        rounds.bootstrap(self.business,self.current,self.invoices)
        with core.ledger() as db:
            saved=[dict(row) for row in db.execute('SELECT * FROM group_b_rounds ORDER BY id')]
            positions=[dict(row) for row in db.execute('SELECT * FROM group_b_round_positions')]
            refunds=[dict(row) for row in db.execute('SELECT * FROM group_b_round_refunds')]
        self.assertEqual([row['id'] for row in saved],[rounds.ROUND_ONE,rounds.ROUND_TWO])
        self.assertEqual(sum(row['role']=='evelyn_invoice' for row in positions),3)
        self.assertEqual(sum(row['role']=='zero_pair' for row in positions),1)
        self.assertEqual(len(refunds),2)
        late=next(row for row in refunds if self.business.loc[self.business.position_key==row['refund_key'],'Bestellnummer'].iloc[0]=='r1-nb')
        self.assertEqual(late['origin_round_id'],rounds.ROUND_ONE)
        self.assertEqual(late['settlement_round_id'],rounds.ROUND_TWO)
        with core.ledger() as db:
            self.assertEqual(rounds.next_round_id(db,2026),'GB-2026-003')

        extra=self.business[(self.business.Bestellnummer=='r2-zero')&(self.business.Art=='Bestellung')]
        with core.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO group_b_round_positions VALUES(?,?,?,?)',
                           (extra.iloc[0].position_key,rounds.ROUND_ONE,'zero_pair','duplicate'))
            db.rollback()

    def test_round_two_never_grows_and_partner_invoice_can_span_rounds(self):
        rounds.bootstrap(self.business,self.current,self.invoices)
        before=rounds.overview(self.business)
        expanded=self.business[(self.business.Art=='Bestellung') & self.business.Bestellnummer.isin(['r2-mh','r2-zero'])]
        rounds.bootstrap(self.business,expanded,self.invoices)
        after=rounds.overview(self.business)
        self.assertEqual([(r['round_id'],r['evelyn_invoiced']) for r in before['rounds']],
                         [(r['round_id'],r['evelyn_invoiced']) for r in after['rounds']])

        chosen=self.business[(self.business.Art=='Bestellung') & self.business.Bestellnummer.isin(['r1-mh','r2-mh'])]
        with core.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT INTO partner_invoices VALUES('multi','hash-multi','MH',NULL,'{}')")
            rounds.link_partner_invoice(db,'multi',chosen)
            related={row[0] for row in db.execute("SELECT round_id FROM partner_invoice_rounds WHERE invoice_id='multi'")}
            db.rollback()
        self.assertEqual(related,{rounds.ROUND_ONE,rounds.ROUND_TWO})

    def test_variant_b_round_totals_keep_full_pair_at_zero(self):
        rounds.bootstrap(self.business,self.current,self.invoices)
        view=rounds.overview(self.business)
        r2_mh=next(row for row in view['partners'] if row['round_id']==rounds.ROUND_TWO and row['partner']=='MH')
        regular=self.business[(self.business.Bestellnummer=='r2-mh')&(self.business.Art=='Bestellung')]
        from partner_export import prepare_partner_export
        expected=prepare_partner_export(regular)['totals']['Rechnung']['gross']
        pair=self.business[self.business.Bestellnummer=='r2-zero']
        pair_model=prepare_partner_export(pair)
        self.assertEqual(r2_mh['current'],expected)
        self.assertEqual(pair_model['totals']['Rechnung']['gross']+
                         pair_model['totals']['Gutschriften']['gross'],Decimal('0.00'))
        self.assertEqual(r2_mh['corrections'],pair_model['totals']['Gutschriften']['gross'])
        self.assertEqual(r2_mh['positive'],expected+pair_model['totals']['Rechnung']['gross'])


if __name__ == '__main__':
    unittest.main()
