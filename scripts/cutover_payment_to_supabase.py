"""One-shot, verified migration. The source directory is never modified."""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import supabase_store

FILES={
    'Master_Orders.csv':'source/orders.csv',
    'Master_Payouts.csv':'source/payouts.csv',
    'Settlement_State.sqlite3':'state/settlement.sqlite3',
    'Settlement_API_Holds.json':'state/holds.json',
    'Settlement_Ebay_Sync.json':'state/ebay_sync.json',
    'Settlement_Payout_Reconciliation.json':'state/reconciliation.json',
    'Settlement_Partner_Invoices.json':'state/partner_invoices.json',
    'billing_recipients.json':'config/billing_recipients.json',
}

def digest(data): return hashlib.sha256(data).hexdigest()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('source',type=Path)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    source=args.source.resolve()
    before={str(p.relative_to(source)):(p.stat().st_size,p.stat().st_mtime_ns,digest(p.read_bytes()))
            for p in source.rglob('*') if p.is_file()}
    objects={}
    for filename,key in FILES.items():
        path=source/filename
        if path.exists(): objects[key]=path.read_bytes()
    # The reviewed code configuration contains the MAH- -> FS correction.
    objects['config/partners.json']=(ROOT/'partners.json').read_bytes()
    latest=source/'Ebay_Readonly'/'latest.json'
    if latest.exists(): objects['state/trust_risk/latest.json']=latest.read_bytes()
    for path in sorted((source/'Partner_Invoices').glob('*')) if (source/'Partner_Invoices').exists() else []:
        if path.is_file(): objects['partner-invoices/'+path.name]=path.read_bytes()
    required={'source/orders.csv','source/payouts.csv','state/settlement.sqlite3','config/partners.json'}
    missing=required-set(objects)
    if missing: raise SystemExit('Pflichtobjekte fehlen: '+', '.join(sorted(missing)))
    db=supabase_store.sqlite_from_bytes(objects['state/settlement.sqlite3'])
    counts={name:db.execute(f'select count(*) from {name}').fetchone()[0] for name in
            ('payouts','position_workflow','partner_invoices','partner_invoice_positions','discarded_invoices','audit')}
    db.close()
    report={'source':str(source),'objects':{k:{'bytes':len(v),'sha256':digest(v)} for k,v in objects.items()},'sqlite_counts':counts}
    if args.apply:
        supabase_store.ensure_schema()
        for key,data in objects.items():
            current,version=supabase_store.get(key,required=False)
            if current!=data: supabase_store.put(key,data,version)
            remote,_=supabase_store.get(key)
            if remote!=data: raise RuntimeError('Verifikation fehlgeschlagen: '+key)
        manifest=json.dumps(report,ensure_ascii=False,sort_keys=True).encode()
        _,version=supabase_store.get('migration/manifest.json',required=False)
        supabase_store.put('migration/manifest.json',manifest,version)
        report['applied']=True
    after={str(p.relative_to(source)):(p.stat().st_size,p.stat().st_mtime_ns,digest(p.read_bytes()))
           for p in source.rglob('*') if p.is_file()}
    if before!=after: raise RuntimeError('Quelldaten wurden während der Migration verändert; Abbruch.')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
