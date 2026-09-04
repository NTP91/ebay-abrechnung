"""Resumable, serialized Finances ingestion through the existing CSV importer."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from filelock import FileLock, Timeout
import core
import api_holds
from ebay_readonly import Client, EbayError

FILE = 'Settlement_Ebay_Sync.json'


def utc():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def load(directory):
    copies=[]; directory=Path(directory)
    if (directory/FILE).exists():
        copies.append(json.loads((directory/FILE).read_text(encoding='utf-8')))
    database=directory/'Settlement_State.sqlite3'
    if database.exists():
        with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='ebay_sync_state'").fetchone():
                row=db.execute('SELECT document FROM ebay_sync_state WHERE id=1').fetchone()
                if row: copies.append(json.loads(row[0]))
    if not copies:
        return dict(version=0,watermark=None,payouts={},transactions={},runs=[])
    if any(not isinstance(c.get('version'),int) or not isinstance(c.get('runs'),list) for c in copies):
        raise ValueError('API-Importhistorie beschädigt; Sicherung prüfen.')
    latest=[c for c in copies if c['version']==max(c['version'] for c in copies)]
    if any(c!=latest[0] for c in latest):
        raise ValueError('API-Importhistorie widersprüchlich; Sicherung prüfen.')
    return latest[0]


def save(directory, document):
    document['version']+=1
    text=json.dumps(document,ensure_ascii=False)
    directory=Path(directory)
    with closing(sqlite3.connect(directory/'Settlement_State.sqlite3')) as db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS ebay_sync_state (id INTEGER PRIMARY KEY, document TEXT NOT NULL)')
        db.execute('INSERT OR REPLACE INTO ebay_sync_state VALUES(1,?)',(text,));db.commit()
    temporary=(directory/FILE).with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8') as out:
        out.write(text);out.flush();os.fsync(out.fileno())
    os.replace(temporary,directory/FILE)


def eur(value):
    try:
        result=Decimal(str(value['value']))
        if value['currency']!='EUR' or not result.is_finite() or result<0: raise ValueError()
        return result
    except (KeyError,TypeError,InvalidOperation,ValueError):
        raise ValueError('API enthält keinen gültigen EUR-Betrag.') from None


def signed(row):
    if row.get('bookingEntry') not in ('CREDIT','DEBIT'):
        raise ValueError('API-Bewegung ohne eindeutiges CREDIT/DEBIT.')
    return eur(row['amount'])*(1 if row['bookingEntry']=='CREDIT' else -1)


def identity(row):
    if not row.get('transactionType') or not row.get('transactionId'):
        raise ValueError('API-Bewegung ohne eindeutige Identität.')
    return row['transactionType']+'|'+row['transactionId']


def date_text(value):
    stamp=api_holds.stamp(value)
    if stamp is None: raise ValueError('API-Bewegung ohne gültiges Datum.')
    return stamp.strftime('%d.%m.%Y')


def decimal_text(value):
    return format(value,'.2f').replace('.',',')


def validate_payout(detail, transactions):
    pid=detail.get('payoutId')
    rows=transactions['items']
    if not pid or len(rows)!=detail.get('transactionCount') or len({identity(r) for r in rows})!=len(rows):
        raise ValueError('API-Payout unvollständig: ID, Anzahl oder Transaktionsidentität abweichend.')
    if any(r.get('payoutId')!=pid for r in rows):
        raise ValueError('API-Bewegung gehört nicht zum abgefragten Payout.')
    total=sum((signed(r) for r in rows),Decimal(0))
    if total!=eur(detail['amount']):
        raise ValueError(f'API-Payout {pid} nicht abgestimmt: Differenz {total-eur(detail["amount"])} EUR.')
    return total


def references(row):
    return {str(r['referenceId']) for r in row.get('references',[]) if r.get('referenceId')}


def adapt(rows, payouts, raw, orders):
    """Match legacy CSV first; never guess a second financial booking for an API ID."""
    frames=[]; known=0; ledger_only=0; matched_legacy={}
    for transaction in rows:
        key=identity(transaction);kind=transaction['transactionType'];pid=transaction.get('payoutId','')
        if kind not in ('SALE','REFUND','NON_SALE_CHARGE'):
            # Holds, releases, funding and other movements stay in the complete API ledger.
            ledger_only+=1;continue
        if pid and payouts[pid]['payoutStatus']!='SUCCEEDED':
            ledger_only+=1;continue
        if pid and transaction.get('transactionStatus')!='PAYOUT':
            raise ValueError('Payout-Bewegung noch nicht final; Import bleibt offen.')
        value=signed(transaction)
        if kind=='SALE' and value<=0 or kind=='REFUND' and value>=0:
            raise ValueError('Ungewöhnliches Vorzeichen einer Verkaufs-/Erstattungsbewegung.')
        order=transaction.get('orderId','')
        if kind in ('SALE','REFUND') and not order:
            raise ValueError('Verkaufs-/Erstattungsbewegung ohne Bestellbezug.')
        typ={'SALE':'Bestellung','REFUND':'Rückerstattung','NON_SALE_CHARGE':'Andere Gebühr'}[kind]
        candidates=raw[(raw['Auszahlung Nr.']==pid)&(raw.Bestellnummer==order)&(raw.Typ.str.casefold()==typ.casefold())]
        def same_amount(a):
            if not core.clean(a):return False
            try:return core.parse_money(a)==value
            except ValueError:
                if pid:raise
                return False  # Existing open placeholder is resolved by the normal identity merge.
        candidates=candidates.loc[candidates['Betrag abzügl. Kosten'].map(same_amount).astype(bool)]
        ref=references(transaction)
        exact=candidates.loc[candidates.Referenznummer.map(lambda r: transaction['transactionId']==r or bool(ref & set(re.findall(r'\d+',r)))).astype(bool)] if 'Referenznummer' in candidates else candidates.iloc[0:0]
        if not exact.empty: candidates=exact
        lines=transaction.get('orderLineItems',[]) if kind in ('SALE','REFUND') else []
        line_ids={str(line['lineItemId']) for line in lines if line.get('lineItemId')}
        if len(line_ids)==1:
            exact=candidates[candidates.Transaktionsnummer.isin(line_ids)]
            if not exact.empty:candidates=exact
        if len(candidates)>1:
            raise ValueError(f'API/CSV-Zuordnung für {order or key} mehrdeutig; keine Doppelanlage.')
        if len(candidates)==1:
            index=candidates.index[0]
            if index in matched_legacy and matched_legacy[index]!=key:
                raise ValueError('Mehrere API-Bewegungen passen auf dieselbe CSV-Zeile; Prüfung erforderlich.')
            matched_legacy[index]=key;known+=1;continue
        gross=value
        if kind=='SALE':
            gross=eur(transaction['totalFeeBasisAmount'])
            if lines and sum((eur(line['feeBasisAmount']) for line in lines),Decimal(0))!=gross:
                raise ValueError('API-Artikelbeträge stimmen nicht mit dem Bestellgesamtbetrag überein.')
        elif kind=='REFUND' and transaction.get('totalFeeBasisAmount'):
            gross=-eur(transaction['totalFeeBasisAmount'])
        tx=next(iter(line_ids)) if len(line_ids)==1 else ''
        # Parent = one financial row. All article references remain in API_Artikelreferenzen.
        item='';title='';sku=''
        matches=orders[orders.Transaktionsnummer==tx] if tx else orders[orders.Bestellnummer==order]
        if len(matches)==1 and matches.iloc[0].Bestellnummer==order:
            item=matches.iloc[0].Artikelnummer;title=matches.iloc[0].Angebotstitel;sku=matches.iloc[0].SKU
        if len(line_ids)>1:item=''
        if kind=='REFUND':tx=transaction['transactionId']
        reference=next(iter(sorted(ref))) if ref else transaction['transactionId']
        record={'Datum':date_text(transaction['transactionDate']),'Auszahlung Nr.':pid,
                'Typ':typ,'Bestellnummer':order,'Transaktionsnummer':tx,'Artikelnummer':item,
                'SKU':sku,'Angebotstitel':title,'Betrag abzügl. Kosten':decimal_text(value),
                'Transaktionsbetrag (inkl. Kosten)':decimal_text(gross),
                'Auszahlungsdatum':date_text(payouts[pid]['payoutDate']) if pid else '',
                'Auszahlungsstatus':'Betrag überwiesen' if pid else '', 'Referenznummer':reference,
                'API_Transaktion':key,'API_Artikelreferenzen':json.dumps(lines,ensure_ascii=False),'Importquelle':'eBay API'}
        frames.append(record)
    return core.canonicalize(core.pd.DataFrame(frames)) if frames else None, known, ledger_only


def run(directory, trigger='manual', client=None, now=None):
    """Shared automatic/manual path. Failed attempts do not advance the checkpoint."""
    if trigger not in ('manual','automatic'):raise ValueError('Ungültige Abrufquelle.')
    directory=Path(directory).resolve()
    if Path(core.PAYOUTS_DB_PATH).resolve().parent!=directory:
        raise ValueError('API-Import und Abrechnungsbestand zeigen auf verschiedene Verzeichnisse.')
    directory.mkdir(parents=True,exist_ok=True)
    try:
        lock=FileLock(str(directory/FILE)+'.lock',timeout=0);lock.acquire()
    except Timeout:
        return {'status':'busy','error':'Ein API-Import läuft bereits.'}
    try:
        document=load(directory);end=now or utc()
        previous=api_holds.stamp(document['watermark'])
        start=(previous-timedelta(days=7)) if previous else end-timedelta(days=90)
        if previous is None:
            from partner_export import report_date
            raw_start=core.read_master(core.PAYOUTS_DB_PATH)
            dates=[]
            for value in raw_start.get('Auszahlungsdatum',[]):
                try:
                    parsed=report_date(value)
                    if parsed:dates.append(parsed.replace(tzinfo=timezone.utc))
                except ValueError:pass
            if dates:start=min(max(dates),end)-timedelta(days=7)
            if document['runs']:
                start=min(start,*(api_holds.stamp(r['start']) for r in document['runs']))
        run_record=dict(id=uuid.uuid4().hex,source='eBay API',trigger=trigger,at=iso(end),start=iso(start),end=iso(end),
                        status='running',new_payouts=0,new_transactions=0,known=0,ledger_only=0,error='')
        for old in document['runs']:
            if old['status']=='running':old.update(status='interrupted',error='Vorheriger Lauf unterbrochen; Datenfenster wird erneut verarbeitet.')
        document['runs'].append(run_record);save(directory,document)
        client=client or Client()
        try:
            # Chunk catch-up windows; never discard an old successful checkpoint.
            found={}; transactions={};cursor=start
            while cursor<end:
                stop=min(cursor+timedelta(days=89),end)
                for p in client.pages('payouts','payouts',{'filter':f'payoutDate:[{iso(cursor)}..{iso(stop)}]'})['items']:
                    found[p['payoutId']]=p
                for row in client.pages('transactions','transactions',{'filter':f'transactionDate:[{iso(cursor)}..{iso(stop)}]'})['items']:
                    transactions[identity(row)]=row
                cursor=stop
            # Older pending payouts and transactions must not fall out of the time window.
            raw=core.read_master(core.PAYOUTS_DB_PATH);orders=core.read_master(core.ORDERS_DB_PATH)
            ids=set(found)|set(raw['Auszahlung Nr.'])-{''}
            ids|={r.get('payoutId') for r in transactions.values() if r.get('payoutId')}
            ids|={pid for pid,p in document['payouts'].items() if p.get('payoutStatus')!='SUCCEEDED'}
            held_orders=set(api_holds.active(api_holds.load(directory)))
            held_orders|={r.get('orderId') for r in document['transactions'].values() if r.get('orderId') and r.get('transactionStatus')!='PAYOUT'}
            for order in sorted(held_orders):
                if not re.fullmatch(r'[A-Za-z0-9-]+',order):raise ValueError('Ungültige API-Bestellreferenz.')
                for row in client.pages('transactions','transactions',{'filter':f'orderId:{{{order}}}'})['items']:
                    transactions[identity(row)]=row
                    if row.get('payoutId'):ids.add(row['payoutId'])
            details={};payout_rows={}
            for pid in sorted(ids):
                detail=client.get('payout',pid)
                movements=client.pages('transactions','transactions',{'filter':f'payoutId:{{{pid}}}'})
                validate_payout(detail,movements)
                details[pid]=detail;payout_rows[pid]=movements
                for row in movements['items']:transactions[identity(row)]=row
            # Validate/adapt everything before the first source-store write.
            frame,known,ledger_only=adapt(list(transactions.values()),details,raw,orders)
            evidence={'account':'ebay_durchstart','fetched_at':iso(end),'resources':{'transactions':{'available':True,'data':{'items':list(transactions.values())}}}}
            api_holds.ingest(directory,evidence)
            counters={}
            if frame is not None:
                core.import_reports([frame],core.PAYOUTS_DB_PATH,'payout',details=counters)
            warnings=counters.get('warnings',[])
            run_record.update(new_payouts=len(set(details)-set(document['payouts'])-set(raw['Auszahlung Nr.'])),
                              new_transactions=counters.get('new_paid',0)+counters.get('new_open',0),
                              known=known+counters.get('known_paid',0)+counters.get('still_open',0),ledger_only=ledger_only)
            # Complete API ledger includes every bank movement, not only partner rows.
            document['transactions'].update(transactions);document['payouts'].update(details)
            if warnings:
                run_record.update(status='partial',error='; '.join(w['payout']+': '+w['reason'] for w in warnings))
            else:
                run_record['status']='success';document['watermark']=iso(end)
            # Keep existing Trust/Risk resources, refresh finances through this same path.
            import trust_risk
            cached=trust_risk.load_snapshot(directory) or dict(version=1,account='ebay_durchstart',resources={})
            cached.setdefault('fetched_at',iso(end))
            cached.update(finances_fetched_at=iso(end),transaction_window_start=iso(start))
            cached['resources']['transactions']=evidence['resources']['transactions']
            cached['resources']['payouts']={'available':True,'data':{'items':list(details.values())}}
            if trust_risk.PAYOUT in details:
                cached['resources']['reference_payout']={'available':True,'data':details[trust_risk.PAYOUT]}
                cached['resources']['reference_transactions']={'available':True,'data':payout_rows[trust_risk.PAYOUT]}
            trust_risk.save_snapshot(directory,cached)
        except (EbayError,ValueError,OSError,KeyError,TypeError) as exc:
            # No tokens or raw response bodies in the durable error log.
            run_record.update(status='failed',error=client.redact(str(exc)) if isinstance(exc,(EbayError,ValueError)) else 'API-Import unvollständig: Datenformat oder lokaler Schreibzugriff fehlgeschlagen.')
            document['watermark']=iso(previous) if previous else None
        run_record['finished_at']=iso(utc());save(directory,document)
        return run_record
    finally:
        lock.release()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',required=True)
    parser.add_argument('--trigger',choices=['automatic','manual'],default='automatic')
    args=parser.parse_args()
    import subprocess
    branch=subprocess.run(['git','branch','--show-current'],cwd=Path(__file__).parent,capture_output=True,text=True,check=True).stdout.strip()
    if branch!='codex/recover-payout-settlement':
        print('API-Import nur auf dem Recovery-Branch erlaubt.')
        return 1
    core.PAYOUTS_DB_PATH=str(Path(args.data_dir)/'Master_Payouts.csv')
    core.ORDERS_DB_PATH=str(Path(args.data_dir)/'Master_Orders.csv')
    result=run(args.data_dir,args.trigger)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status']=='success' else 1


if __name__=='__main__':raise SystemExit(main())
