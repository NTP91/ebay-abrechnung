import unittest
import pandas as pd

import trust_risk_cases as cases
from ebay_trading import merge_messages


class TrustRiskCaseTests(unittest.TestCase):
    def catalogue(self):
        return pd.DataFrame([{'Bestellnummer':'o1','Transaktionsnummer':'t1','Artikelnummer':'i1',
          'SKU':'MH / X','Partner':'MH','Produkttitel':'Produkt'}])

    def empty_snapshot(self):
        return {'resources':{'returns':{'data':{'items':[]}},'disputes':{'data':{'items':[]}},'transactions':{'data':{'items':[]}}}}

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
        self.assertEqual(len(model['signals']),2)
        self.assertEqual(len(model['unmatched']),0)

    def test_seller_reply_does_not_create_case(self):
        snap={'resources':{'returns':{'data':{'items':[]}},'disputes':{'data':{'items':[]}},'transactions':{'data':{'items':[]}}}}
        model=cases.build(snap,self.catalogue(),{'feedback':[],'messages':[{'message_id':'m','item_id':'i1','text':'Artikel war kaputt','sender_role':'seller'}]})
        self.assertEqual(model['cases'],[])

    def test_message_order_id_resolves_line_sku_and_partner_without_sku_in_text(self):
        message={'message_id':'m','subject':'Frage zu Bestellung 04-15090-66849','text':'Der Artikel ist kaputt','sender_role':'buyer'}
        catalogue=pd.DataFrame([{'Bestellnummer':'04-15090-66849','Transaktionsnummer':'line','Artikelnummer':'item',
          'SKU':'MH / A','Partner':'MH','Produkttitel':'Titel'}])
        model=cases.build(self.empty_snapshot(),catalogue,{'feedback':[],'messages':[message]})
        self.assertEqual((model['cases'][0]['line_item_id'],model['cases'][0]['sku'],model['cases'][0]['partner_id']),('line','MH / A','MH'))

    def test_multi_line_order_requires_item_context(self):
        catalogue=pd.DataFrame([
          {'Bestellnummer':'18-15098-91741','Transaktionsnummer':'t1','Artikelnummer':'i1','SKU':'MH / A','Partner':'MH','Produkttitel':'A'},
          {'Bestellnummer':'18-15098-91741','Transaktionsnummer':'t2','Artikelnummer':'i2','SKU':'MH / B','Partner':'MH','Produkttitel':'B'}])
        message={'message_id':'m','subject':'Bestellung 18-15098-91741','text':'Artikel ist kaputt','sender_role':'buyer'}
        model=cases.build(self.empty_snapshot(),catalogue,{'feedback':[],'messages':[message]})
        self.assertEqual(model['cases'],[])
        self.assertEqual(model['unmatched'][0]['reason'],'Mehrere Line Items; kein eindeutiger Artikelbezug')
        message['item_id']='i2'
        model=cases.build(self.empty_snapshot(),catalogue,{'feedback':[],'messages':[message]})
        self.assertEqual(model['cases'][0]['sku'],'MH / B')

    def test_unclear_message_without_order_context_stays_unmatched(self):
        message={'message_id':'m','text':'Der Artikel ist kaputt','sender_role':'buyer'}
        model=cases.build(self.empty_snapshot(),self.catalogue(),{'feedback':[],'messages':[message]})
        self.assertEqual(model['unmatched'][0]['reason'],'Keine Order-ID oder Artikelreferenz vorhanden')

    def test_generic_message_with_order_is_linked_but_not_quality_problem(self):
        message={'message_id':'m','subject':'Bestellung o1','order_id':'o1','text':'Danke','sender_role':'buyer'}
        model=cases.build(self.empty_snapshot(),self.catalogue(),{'feedback':[],'messages':[message]})
        self.assertEqual(len(model['signals']),1)
        self.assertFalse(model['cases'][0]['is_problem'])

    def test_buyer_and_item_resolve_order_from_fulfillment_context(self):
        snap=self.empty_snapshot();snap['resources']['orders']={'data':{'items':[{
          'orderId':'o1','buyer':{'username':'kunde'},
          'lineItems':[{'lineItemId':'t1','legacyItemId':'i1'}]}]}}
        message={'message_id':'m','item_id':'i1','sender':'KUNDE','text':'Der Artikel ist kaputt','sender_role':'buyer'}
        model=cases.build(snap,self.catalogue(),{'feedback':[],'messages':[message]})
        self.assertEqual(model['cases'][0]['order_id'],'o1')
        self.assertEqual(model['cases'][0]['sku'],'MH / X')

    def test_message_sources_are_deduplicated(self):
        base={'message_id':'same','item_id':'i','text':'kaputt','sender':'buyer','received_at':'2026-09-01T10:00:00Z'}
        self.assertEqual(len(merge_messages([base],[base])),1)
        other={**base,'message_id':'other'}
        self.assertEqual(len(merge_messages([base],[other])),1)

    def test_wrong_variant_is_not_automatically_wrong_item(self):
        self.assertEqual(cases.category('WRONG_SIZE',''),'wrong_variant')
        self.assertEqual(cases.category('','falschen Artikel erhalten'),'wrong_item')

    def test_payment_dispute_attaches_to_existing_case_identity(self):
        snap=self.empty_snapshot();snap['resources']['disputes']['data']['items']=[{
          'paymentDisputeId':'d','orderId':'o1','orderLineItemId':'t1','reason':'ITEM_NOT_RECEIVED'}]
        model=cases.build(snap,self.catalogue(),{'feedback':[],'messages':[]})
        self.assertTrue(model['cases'][0]['has_dispute'])

    def test_hold_is_not_a_quality_problem(self):
        snap=self.empty_snapshot();snap['resources']['transactions']['data']['items']=[{
          'transactionId':'h','transactionType':'DISPUTE','transactionStatus':'FUNDS_ON_HOLD',
          'orderId':'o1','references':[{'referenceType':'TRANSACTION_ID','referenceId':'t1'}]}]
        model=cases.build(snap,self.catalogue(),{'feedback':[],'messages':[]})
        self.assertTrue(model['cases'][0]['has_hold'])
        self.assertFalse(model['cases'][0]['is_problem'])
        self.assertEqual(cases.summarize(model)['cases'],0)


if __name__=='__main__': unittest.main()
