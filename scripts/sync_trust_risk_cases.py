"""Fetch read-only eBay evidence and upsert the case-centric model to Supabase."""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import core
import trust_risk
from ebay_readonly import Client, EbayError
from ebay_trading import TradingClient
from trust_risk_cases import build, summarize


class Management:
    def __init__(self):
        self.token=os.environ['SUPABASE_ACCESS_TOKEN'].strip(); self.ref=os.environ['SUPABASE_PROJECT_REF'].strip()
        self.base=f'https://api.supabase.com/v1/projects/{self.ref}/database'
        self.headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'}
    def read(self,sql):
        r=requests.post(self.base+'/query/read-only',headers=self.headers,json={'query':sql,'parameters':[]},timeout=90)
        if r.status_code not in (200,201): raise RuntimeError(f'Supabase read failed HTTP {r.status_code}')
        value=r.json(); return value if isinstance(value,list) else value.get('result',[])
    def migration_names(self):
        r=requests.get(self.base+'/migrations',headers={'Authorization':'Bearer '+self.token},timeout=45)
        if r.status_code!=200: raise RuntimeError(f'Supabase migration history failed HTTP {r.status_code}')
        return {x.get('name') for x in r.json()}
    def apply(self,name,sql):
        r=requests.post(self.base+'/migrations',headers=self.headers,json={'query':sql,'name':name},timeout=180)
        if r.status_code not in (200,201):
            detail=''
            try: detail=str(r.json().get('message') or r.json().get('error') or '')
            except ValueError: pass
            raise RuntimeError(f'Supabase migration {name} failed HTTP {r.status_code}: {detail[:300]}')


def encoded(value):
    return base64.b64encode(json.dumps(value,ensure_ascii=False,default=str).encode()).decode()


def recordset_sql(table, rows, columns, conflict):
    if not rows: return ''
    payload=encoded([{k:r.get(k) for k in columns} for r in rows])
    typed=[]
    bools={'has_return','has_message','has_dispute','has_hold','has_negative_feedback',
           'not_as_described','wrong_item','defective','used_instead_of_new','opened_used','empty_consumed',
           'incomplete_parts','wrong_variant','item_not_received','other_complaint','is_problem','reply_present','attachment_present'}
    jsons={'raw_payload','payload'}; stamps={'event_at','first_event_at','last_contact_at'}
    for col in columns:
        kind='boolean' if col in bools else 'jsonb' if col in jsons else 'timestamptz' if col in stamps else 'text'
        typed.append(f'{col} {kind}')
    updates=','.join(f'{c}=excluded.{c}' for c in columns if c not in conflict)
    return (f"insert into public.{table}({','.join(columns)}) select {','.join(columns)} from "
            f"jsonb_to_recordset(convert_from(decode('{payload}','base64'),'UTF8')::jsonb) as x({','.join(typed)}) "
            f"on conflict({','.join(conflict)}) do update set {updates};")


def apply_batches(management, prefix, table, rows, columns, conflict, size=20):
    for number, offset in enumerate(range(0,len(rows),size)):
        sql='begin;'+recordset_sql(table,rows[offset:offset+size],columns,conflict)+'commit;'
        management.apply(f'{prefix}_{table}_{number:03d}',sql)
        time.sleep(1.1)  # Supabase migration versions are second-granular.


def main():
    management=Management()
    migration=ROOT/'supabase/migrations/20260906090009_audit_cases.sql'
    name=migration.stem
    if name not in management.migration_names(): management.apply(name,migration.read_text(encoding='utf-8'))
    rest=Client(); stamp=datetime.now(timezone.utc); orders=core.read_master(core.ORDERS_DB_PATH)
    catalogue=orders.copy()
    catalogue['Partner']=catalogue['SKU'].map(core.normalized_partner)
    catalogue['Produkttitel']=catalogue['Angebotstitel']
    known={'001','BA','MK','PP','MH',*core.known_group_b_partners()}
    catalogue=catalogue[catalogue['Partner'].isin(known)].copy()
    snapshot=trust_risk.collect(rest)
    trading=TradingClient(rest)
    coverage={}
    sources={}
    for key,job in [('GetMyMessages',trading.my_messages),('GetMemberMessages',trading.member_messages),('feedback',trading.negative_feedback)]:
        try:
            rows=job(); sources[key]=rows; coverage[key]={'available':True,'count':len(rows)}
        except EbayError as exc:
            sources[key]=[]; coverage[key]={'available':False,'error':str(exc)}
    # GetMyMessages covers the mailbox folders (Inbox, Sent, Deleted) and more
    # message types. GetMemberMessages is the fallback for buyer questions on
    # active listings when the mailbox endpoint is unavailable.
    selected=('GetMyMessages' if coverage['GetMyMessages'].get('available') and sources['GetMyMessages']
              else 'GetMemberMessages' if coverage['GetMemberMessages'].get('available') else None)
    messages=sources.get(selected,[])
    for row in messages:
        row['sender_role']='seller' if row.get('folder_id')=='1' else 'buyer' if row.get('folder_id')=='0' else 'unknown'
    model=build(snapshot,catalogue,{'messages':messages,'feedback':sources['feedback']})
    case_cols=['order_id','line_item_id','sku','partner_id','title','has_return','has_message','has_dispute','has_hold','has_negative_feedback',
      'not_as_described','wrong_item','defective','used_instead_of_new','opened_used','empty_consumed','incomplete_parts','wrong_variant','item_not_received','other_complaint',
      'return_reason_de','buyer_comment','first_event_at','last_contact_at','case_status','is_problem']
    for row in model['signals']:
        row['external_id']=row['source_id']; row['summary']=row.get('category') or ''; row['payload']=row['raw_payload']
        for field in ('status','original_code','original_text','sender','recipient','sender_role'):
            row[field]=row.get(field) or ('unknown' if field=='sender_role' else '')
    for row in model['unmatched']:
        row['external_id']=row['source_id']; row['payload']=row['raw_payload']
    sig_cols=['source','external_id','order_id','line_item_id','sku','partner_id','event_at','status','summary','payload','original_code','original_text','sender','recipient','sender_role','reply_present','attachment_present']
    unmatched_cols=['source','external_id','reason','payload']
    run='sync_trust_risk_'+stamp.strftime('%Y%m%dT%H%M%S')
    apply_batches(management,run,'audit_cases',model['cases'],case_cols,['order_id','line_item_id','sku','partner_id'],50)
    apply_batches(management,run,'audit_case_signals',model['signals'],sig_cols,['source','external_id','order_id','line_item_id','sku','partner_id'],10)
    apply_batches(management,run,'audit_unmatched_signals',model['unmatched'],unmatched_cols,['source','external_id'],10)
    reconcile="""update public.audit_cases c set
      has_return=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.source='return'),
      has_message=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.source='message'),
      has_dispute=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.source='dispute'),
      has_hold=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.source='hold'),
      has_negative_feedback=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.source='negative_feedback'),
      not_as_described=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.summary='not_as_described'),
      wrong_item=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.summary='wrong_item'),
      defective=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.summary='defective'),
      used_instead_of_new=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.summary='used_instead_of_new'),
      opened_used=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.summary='opened_used'),
      empty_consumed=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.summary='empty_consumed'),
      incomplete_parts=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.summary='incomplete_parts'),
      wrong_variant=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.summary='wrong_variant'),
      item_not_received=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.summary='item_not_received'),
      other_complaint=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id and s.summary='other_complaint'),
      is_problem=exists(select 1 from public.audit_case_signals s where s.order_id=c.order_id and s.line_item_id=c.line_item_id and s.sku=c.sku and s.partner_id=c.partner_id);"""
    management.apply(run+'_reconcile',reconcile)
    counts={k:len(((snapshot['resources'].get(k) or {}).get('data') or {}).get('items',[])) for k in ('returns','disputes','transactions')}
    result={'applied':True,**summarize(model),'signals':len(model['signals']),'unmatched':len(model['unmatched']),
            'coverage':{**coverage,'selected_message_api':selected,**counts}}
    print(json.dumps(result,ensure_ascii=False,default=list))


if __name__=='__main__':
    main()
