import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import core
import data_status
import position_workflow
import studio_view
from test_recovery import payout, Upload


class HistoricalWithoutSkuTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        paths=patch.multiple(core,PAYOUTS_DB_PATH=str(Path(temp.name)/'Master_Payouts.csv'),ORDERS_DB_PATH=str(Path(temp.name)/'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)

    def test_order_report_counts_missing_sku_as_archived_not_issue(self):
        rows=[payout(transaction=f't{i}',order=f'o{i}',sku='',title='Historischer Titel') for i in range(23)]
        rows.append(payout(transaction='current',order='current',sku='NB / 1',title='Aktuell'))
        upload=Upload(core.pd.concat(rows).to_csv(sep=';',index=False).encode('utf-8'),'orders.csv')
        receipt=data_status.import_file(upload,'orders')
        self.assertEqual(receipt['historical_without_sku'],23)
        self.assertEqual(receipt['issues'],0)
        self.assertEqual(len(core.read_master(core.ORDERS_DB_PATH)),24)

    def test_matching_payout_without_order_sku_disappears_from_operations(self):
        historical=payout(sku='',title='Payouttitel darf keine Partnerquelle sein')
        order=payout(sku='',title='Historischer Titel')
        core.import_reports([order],core.ORDERS_DB_PATH,'orders')
        core.import_reports([historical],core.PAYOUTS_DB_PATH,'payout')
        master=core.load_master_data()
        self.assertTrue(master.empty)
        self.assertTrue(core.sync_status(master).empty)
        self.assertTrue(position_workflow.positions(master).empty)
        catalogue=studio_view.order_catalogue(core.read_master(core.PAYOUTS_DB_PATH),master)
        self.assertTrue(catalogue.empty)

    def test_valid_sku_and_unrelated_payout_issue_remain_visible(self):
        valid=payout(transaction='valid',order='valid',sku='BA / 1')
        unknown=payout('p2',transaction='',order='',sku='',title='')
        unknown['Typ']='Auszahlung'
        core.import_reports([valid],core.ORDERS_DB_PATH,'orders')
        core.import_reports([valid,unknown],core.PAYOUTS_DB_PATH,'payout')
        master=core.load_master_data()
        self.assertEqual(len(master),2)
        self.assertEqual(master[master['Prüfhinweis']!=''].iloc[0]['Auszahlung Nr.'],'p2')
        self.assertEqual(int(master['Prüfhinweis'].astype(bool).sum()),1)
        self.assertEqual(len(studio_view.partner_rows(position_workflow.positions(master))),1)


if __name__=='__main__': unittest.main()
