import unittest
import pandas as pd

import trust_risk_cases as cases


class TrustRiskCaseTests(unittest.TestCase):
    def catalogue(self):
        return pd.DataFrame([{'Bestellnummer':'o1','Transaktionsnummer':'t1','Artikelnummer':'i1',
          'SKU':'MH / X','Partner':'MH','Produkttitel':'Produkt'}])

    def test_multiple_signals_form_one_case(self):
        snap={'resources':{
          'returns':{'data':{'items':[{'returnId':'r1','orderId':'o1','creationInfo':{'item':{'transactionId':'t1'},'reason':'NOT_AS_DESCRIBED'}}]}},
          'disputes':{'data':{'items':[{'paymentDisputeId':'d1','orderId':'o1','orderLineItemId':'t1','reason':'NOT_AS_DESCRIBED'}]}},
          'transactions':{'data':{'items':[]}}}}
        model=cases.build(snap,self.catalogue(),{'feedback':[],'messages':[]})
        self.assertEqual(len(model['cases']),1)
        self.assertTrue(model['cases'][0]['has_return'])
        self.assertTrue(model['cases'][0]['has_dispute'])
        self.assertTrue(model['cases'][0]['not_as_described'])

    def test_clear_german_message_is_classified_but_generic_is_not_guessed(self):
        snap={'resources':{'returns':{'data':{'items':[]}},'disputes':{'data':{'items':[]}},'transactions':{'data':{'items':[]}}}}
        messages=[{'message_id':'m1','item_id':'i1','text':'Ich habe den falschen Artikel erhalten','sender_role':'buyer'},
                  {'message_id':'m2','item_id':'i1','text':'Danke für die Lieferung','sender_role':'buyer'}]
        model=cases.build(snap,self.catalogue(),{'feedback':[],'messages':messages})
        self.assertEqual(len(model['cases']),1)
        self.assertTrue(model['cases'][0]['wrong_item'])
        self.assertEqual(len(model['unmatched']),1)

    def test_seller_reply_does_not_create_case(self):
        snap={'resources':{'returns':{'data':{'items':[]}},'disputes':{'data':{'items':[]}},'transactions':{'data':{'items':[]}}}}
        model=cases.build(snap,self.catalogue(),{'feedback':[],'messages':[{'message_id':'m','item_id':'i1','text':'Artikel war kaputt','sender_role':'seller'}]})
        self.assertEqual(model['cases'],[])


if __name__=='__main__': unittest.main()
