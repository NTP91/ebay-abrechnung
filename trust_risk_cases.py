"""Case-centric Trust/Risk model. Pure transformation; no settlement writes."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

FIELDS = ('not_as_described','wrong_item','defective','used_instead_of_new','opened_used',
          'empty_consumed','incomplete_parts','wrong_variant','item_not_received','other_complaint')

LABELS = {
 'not_as_described':'Artikel entspricht nicht der Beschreibung',
 'wrong_item':'Falschlieferung / falscher Artikel', 'defective':'Defekt / beschädigt',
 'used_instead_of_new':'Gebrauchte Ware statt neu', 'opened_used':'Geöffnet / bereits benutzt',
 'empty_consumed':'Leer / Verbrauchsmaterial verbraucht', 'incomplete_parts':'Unvollständig / Teile fehlen',
 'wrong_variant':'Falsche Variante / Größe / Ausführung', 'item_not_received':'Artikel nicht erhalten',
 'other_complaint':'Sonstige Beschwerde'}

PATTERNS = [
 ('wrong_item', r'\bfalsch(?:e[snmr]?|en)?\s+(?:artikel|ware|produkt)|falschliefer'),
 ('defective', r'\b(defekt|kaputt|beschädigt|beschaedigt|zerbrochen|funktioniert nicht)'),
 ('used_instead_of_new', r'gebraucht.{0,30}(?:statt|obwohl).{0,20}neu|nicht neu'),
 ('opened_used', r'\b(geöffnet|geoeffnet|bereits geöffnet|schon geöffnet|benutzt|gebrauchsspuren)'),
 ('empty_consumed', r'\b(leer|verbraucht|toner.{0,15}leer|patrone.{0,15}leer)'),
 ('incomplete_parts', r'\b(unvollständig|unvollstaendig|fehlt|fehlende teile|teile fehlen)'),
 ('wrong_variant', r'\b(falsche[snmr]?\s+(?:variante|größe|groesse|ausführung|ausfuehrung|farbe))'),
 ('item_not_received', r'\b(nicht erhalten|nicht angekommen|nie angekommen|nicht geliefert)'),
 ('not_as_described', r'nicht (?:wie|der) beschr|entspricht nicht|abweichend von der beschreibung'),
]

CODE_CATEGORIES = {
 'NOT_AS_DESCRIBED':'not_as_described', 'ITEM_NOT_AS_DESCRIBED':'not_as_described',
 'WRONG_ITEM':'wrong_item', 'DIFFERENT_ITEM':'wrong_item', 'DAMAGED':'defective',
 'DEFECTIVE_ITEM':'defective', 'DOES_NOT_WORK_OR_DEFECTIVE':'defective',
 'MISSING_PARTS':'incomplete_parts', 'INCOMPLETE':'incomplete_parts',
 'WRONG_SIZE':'wrong_variant', 'WRONG_COLOR':'wrong_variant',
 'ITEM_NOT_RECEIVED':'item_not_received', 'NOT_RECEIVED':'item_not_received'}


def category(code='', text=''):
    normalized = str(code or '').strip().upper()
    if normalized in CODE_CATEGORIES:
        return CODE_CATEGORIES[normalized]
    body = re.sub(r'\s+', ' ', str(text or '').casefold())
    for name, pattern in PATTERNS:
        if re.search(pattern, body):
            return name
    return None


def timestamp(value):
    if isinstance(value, dict):
        value=value.get('value') or value.get('date') or value.get('creationDate')
    return str(value or '').strip() or None


def catalogue_rows(catalogue):
    rows = []
    for _, row in catalogue.iterrows():
        order = str(row.get('Bestellnummer','')).strip()
        sku = str(row.get('SKU','')).strip()
        partner = str(row.get('Partner','')).strip()
        line = str(row.get('Transaktionsnummer','')).strip() or str(row.get('Artikelnummer','')).strip()
        if order and line and sku and partner:
            rows.append({'order_id':order, 'line_item_id':line, 'transaction_id':str(row.get('Transaktionsnummer','')).strip(),
                         'item_id':str(row.get('Artikelnummer','')).strip(), 'sku':sku, 'partner_id':partner,
                         'title':str(row.get('Produkttitel') or row.get('Angebotstitel') or '').strip()})
    unique = {(r['order_id'],r['line_item_id'],r['sku'],r['partner_id']):r for r in rows}
    return list(unique.values())


def _match(ref, catalogue):
    order, line, item = (str(ref.get(k,'') or '').strip() for k in ('order_id','line_item_id','item_id'))
    choices = catalogue
    if order:
        choices = [r for r in choices if r['order_id'] == order]
    if line:
        exact = [r for r in choices if line in (r['line_item_id'], r['transaction_id'])]
        choices = exact
    elif item:
        choices = [r for r in choices if r['item_id'] == item]
    return choices[0] if len(choices) == 1 else None


def build(snapshot, catalogue_frame, trading):
    catalogue = catalogue_rows(catalogue_frame)
    signals, unmatched = [], []

    def add(source, source_id, payload, ref, code='', text='', at='', status='', force=False, extra=None):
        cat = category(code, text)
        if not force and not cat:
            unmatched.append({'source':source,'source_id':str(source_id),'order_id':ref.get('order_id',''),
                              'line_item_id':ref.get('line_item_id',''),'item_id':ref.get('item_id',''),
                              'reason':'Kein eindeutiges Kundenproblem im Text','raw_payload':payload})
            return
        if force and not cat and source != 'hold':
            cat='other_complaint'
        match = _match(ref, catalogue)
        if not match:
            unmatched.append({'source':source,'source_id':str(source_id),'order_id':ref.get('order_id',''),
                              'line_item_id':ref.get('line_item_id',''),'item_id':ref.get('item_id',''),
                              'reason':'Keine eindeutige Artikel-/Partnerzuordnung','raw_payload':payload})
            return
        signals.append({**match,'source':source,'source_id':str(source_id),'event_at':timestamp(at),
                        'original_code':str(code or ''),'original_text':str(text or ''),'category':cat,
                        'status':str(status or ''),'raw_payload':payload, **(extra or {})})

    resources = (snapshot or {}).get('resources', {})
    for row in ((resources.get('returns') or {}).get('data') or {}).get('items',[]):
        creation=row.get('creationInfo') or {}; item=creation.get('item') or {}
        reason=creation.get('reason') or row.get('returnReason') or ''
        comment=row.get('buyerComment') or creation.get('buyerComment') or row.get('comments') or ''
        add('return',row.get('returnId'),row,{'order_id':row.get('orderId',''),'line_item_id':item.get('transactionId',''),'item_id':item.get('itemId','')},
            reason,comment,creation.get('creationDate') or row.get('creationDate'),row.get('status') or row.get('state'),True)
    for row in ((resources.get('disputes') or {}).get('data') or {}).get('items',[]):
        add('dispute',row.get('paymentDisputeId'),row,{'order_id':row.get('orderId',''),'line_item_id':row.get('orderLineItemId',''),'item_id':row.get('itemId','')},
            row.get('reason',''),row.get('buyerComment',''),row.get('openDate') or row.get('creationDate'),row.get('paymentDisputeStatus'),True)
    for row in ((resources.get('transactions') or {}).get('data') or {}).get('items',[]):
        status=row.get('transactionStatus',''); kind=row.get('transactionType','')
        is_hold=status in ('FUNDS_ON_HOLD','FUNDS_PROCESSING') or (kind=='DISPUTE' and row.get('bookingEntry')=='DEBIT')
        if is_hold:
            refs=row.get('references') or []
            line=next((str(x.get('referenceId')) for x in refs if x.get('referenceType') in ('ORDER_LINE_ITEM_ID','TRANSACTION_ID')), '')
            add('hold',str(row.get('transactionId'))+':'+kind,row,{'order_id':row.get('orderId',''),'line_item_id':line,'item_id':''},
                status,row.get('transactionMemo',''),row.get('transactionDate'),status,True)
    for row in trading.get('feedback',[]):
        add('negative_feedback',row.get('feedback_id'),row,{'order_id':'','line_item_id':row.get('order_line_item_id',''),'item_id':row.get('item_id','')},
            row.get('comment_type',''),row.get('comment_text',''),row.get('event_at'),row.get('comment_type'),True)
    for row in trading.get('messages',[]):
        text=' '.join(filter(None,(row.get('subject',''),row.get('text',''))))
        extra={'sender':row.get('sender',''),'recipient':row.get('recipient',''),
               'sender_role':row.get('sender_role','unknown'),'reply_present':bool(row.get('reply_present')),
               'attachment_present':bool(row.get('attachment_present'))}
        # Seller replies are evidence on an existing case, never a new complaint.
        if extra['sender_role']=='seller':
            unmatched.append({'source':'message','source_id':str(row.get('message_id')),'order_id':'',
                              'line_item_id':'','item_id':row.get('item_id',''),
                              'reason':'Verkäuferantwort; kein neuer Kundenfall','raw_payload':row})
            continue
        add('message',row.get('message_id'),row,{'order_id':row.get('order_id',''),'line_item_id':row.get('line_item_id',''),'item_id':row.get('item_id','')},
            '',text,row.get('received_at'),'',False,extra)

    cases={}
    for sig in signals:
        key=tuple(sig[k] for k in ('order_id','line_item_id','sku','partner_id'))
        case=cases.setdefault(key,{k:sig[k] for k in ('order_id','line_item_id','sku','partner_id','title')})
        for field in ('has_return','has_message','has_dispute','has_hold','has_negative_feedback',*FIELDS):
            case.setdefault(field,False)
        case['has_'+sig['source']]=True
        if sig.get('category'): case[sig['category']]=True
        if sig['source']=='return':
            case['return_reason_de']=LABELS.get(sig.get('category'),sig.get('original_code'))
            case['buyer_comment']=sig.get('original_text') or None
        dates=[d for d in (case.get('first_event_at'),sig.get('event_at')) if d]
        if dates: case['first_event_at']=min(dates);case['last_contact_at']=max(dates)
        case['case_status']='geschlossen' if str(sig.get('status','')).upper() in ('CLOSED','RESOLVED') else 'offen'
        case['is_problem']=True
    for case in cases.values():
        case['return_reason_de']=case.get('return_reason_de') or ''
        case['buyer_comment']=case.get('buyer_comment') or ''
    return {'cases':list(cases.values()),'signals':signals,'unmatched':unmatched}


def summarize(model):
    cases=model['cases']; categories={f:sum(bool(c.get(f)) for c in cases) for f in FIELDS}
    partners=Counter(c['partner_id'] for c in cases)
    skus=Counter((c['partner_id'],c['sku']) for c in cases if c['sku'])
    return {'cases':len(cases),'categories':categories,'partners':partners.most_common(),
            'skus':[(p,s,n) for (p,s),n in skus.most_common()]}
