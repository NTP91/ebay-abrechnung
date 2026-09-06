import unittest

import pandas as pd

import trust_risk_reporting as reporting


class TrustRiskReportingTests(unittest.TestCase):
    def cases(self):
        base={name:False for name in reporting.CATEGORIES}
        return pd.DataFrame([
          {**base,'order_id':'o1','line_item_id':'l1','sku':'MH / X','partner_id':'MH','title':'X','is_problem':True,'defective':True,'has_negative_feedback':False,'problem_signal_count':2},
          {**base,'order_id':'o1','line_item_id':'l1','sku':'MH / X','partner_id':'MH','title':'X','is_problem':True,'defective':True,'has_negative_feedback':False,'problem_signal_count':2},
          {**base,'order_id':'o2','line_item_id':'l2','sku':'MH / X','partner_id':'MH','title':'X','is_problem':True,'opened_used':True,'has_negative_feedback':False,'problem_signal_count':1},
          {**base,'order_id':'o3','line_item_id':'l3','sku':'PP / Y','partner_id':'PP','title':'Y','is_problem':True,'other_complaint':True,'has_negative_feedback':True,'problem_signal_count':1},
        ])

    def orders(self):
        return pd.DataFrame([
          {'Bestellnummer':'o1','Transaktionsnummer':'l1','Artikelnummer':'i1','SKU':'MH / X'},
          {'Bestellnummer':'o2','Transaktionsnummer':'l2','Artikelnummer':'i1','SKU':'MH / X'},
          {'Bestellnummer':'o4','Transaktionsnummer':'l4','Artikelnummer':'i1','SKU':'MH / X'},
          {'Bestellnummer':'o3','Transaktionsnummer':'l3','Artikelnummer':'i2','SKU':'PP / Y'},
        ])

    def test_deduplicates_cases_and_calculates_volume_rate(self):
        rows=self.cases()
        neutral=rows.iloc[0].copy();neutral['order_id']='neutral';neutral['is_problem']=False
        result=reporting.aggregate(pd.concat([rows,pd.DataFrame([neutral])],ignore_index=True),self.orders())
        self.assertEqual(result['total'],3)
        mh=result['partners'].set_index('Partner').loc['MH']
        self.assertEqual(mh['Fälle'],2)
        self.assertAlmostEqual(mh['Fälle je 100 Bestellungen'],200/3)

    def test_rankings_groups_and_repeated_sku(self):
        result=reporting.aggregate(self.cases(),self.orders())
        self.assertEqual(result['partners'].iloc[0]['Partner'],'MH')
        self.assertEqual(result['skus'].iloc[0]['Kennzeichnung'],'Wiederholt auffällig')
        self.assertEqual(set(result['groups'].Gruppe),{'Gruppe A','Gruppe B'})

    def test_priority_uses_severity_repetition_signals_and_feedback(self):
        result=reporting.aggregate(self.cases(),self.orders())
        self.assertEqual(len(result['priority']),3)
        self.assertEqual(len(result['negative']),1)
        self.assertIn('Negative Bewertung',result['negative'].iloc[0]['Problemarten'])

    def test_neutral_row_is_excluded_from_aggregation(self):
        row=self.cases().iloc[0].copy();row['is_problem']=False
        result=reporting.aggregate(pd.DataFrame([row]),self.orders())
        self.assertEqual(result['total'],0)


if __name__=='__main__': unittest.main()
