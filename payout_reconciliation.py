"""Manual bank reconciliation. Source rows and existing settlement locks stay intact."""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import core
from payout_structure import validate

STATUSES = ('freigegeben', 'einbehalten', 'unklar')
FILE = 'Settlement_Payout_Reconciliation.json'


def key(row):
    fields = ('Auszahlung Nr.', 'Typ', 'Transaktionsnummer', 'Bestellnummer', 'Artikelnummer', 'Referenznummer')
    return hashlib.sha256(json.dumps([core.clean(row.get(k,'')) for k in fields],ensure_ascii=False).encode()).hexdigest()


def snapshot(row):
    fields = (*core.FIELDS, 'Transaktionsbetrag (inkl. Kosten)', 'Zwischensumme Artikel', 'Verpackung und Versand', 'Referenznummer')
    return hashlib.sha256(json.dumps({k:core.clean(row.get(k,'')) for k in fields},sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def load():
    directory = Path(core.PAYOUTS_DB_PATH).parent
    copies = []
    path = directory/FILE
    if path.exists(): copies.append(json.loads(path.read_text(encoding='utf-8')))
    dbpath = directory/'Settlement_State.sqlite3'
    if dbpath.exists():
        with sqlite3.connect(dbpath.resolve().as_uri()+'?mode=ro',uri=True) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='manual_payout_reconciliation'").fetchone():
                saved = db.execute('SELECT document FROM manual_payout_reconciliation WHERE id=1').fetchone()
                if saved: copies.append(json.loads(saved[0]))
    if not copies: return dict(version=0,payouts={},audit=[])
    for value in copies:
        if not isinstance(value.get('version'),int) or not isinstance(value.get('payouts'),dict):
            raise ValueError('Manueller Payout-Abgleich ist beschädigt; Sicherung prüfen.')
    highest = max(value['version'] for value in copies)
    newest = [v for v in copies if v['version']==highest]
    if any(value != newest[0] for value in newest):
        raise ValueError('Widersprüchliche Payout-Abgleichssicherungen; Freigabe gesperrt.')
    return newest[0]


def rows(payout, raw=None):
    raw = core.read_master(core.PAYOUTS_DB_PATH) if raw is None else raw
    block = raw[raw['Auszahlung Nr.']==str(payout)].copy()
    if block.empty: raise ValueError('Payout nicht vorhanden.')
    children = validate(block)
    financial = block.loc[~block.index.isin(children)].copy()
    financial['abgleich_key'] = financial.apply(key,axis=1)
    if financial.abgleich_key.duplicated().any():
        raise ValueError('Payoutpositionen für manuellen Abgleich nicht eindeutig.')
    return financial, block.loc[block.index.isin(children)].copy()


def inspect(payout, raw=None, document=None):
    document = load() if document is None else document
    financial, children = rows(payout,raw)
    saved = document['payouts'].get(str(payout))
    decisions = saved['items'] if saved else {}
    amounts = [core.parse_money(v) for v in financial['Betrag abzügl. Kosten']]
    statuses=[]; changed=[]
    for _,row in financial.iterrows():
        item=decisions.get(row.abgleich_key)
        stale=bool(item and item['source']!=snapshot(row))
        statuses.append(item['status'] if item and not stale else 'unklar')
        changed.append(stale)
    financial['Abgleichstatus']=statuses
    financial['Quelle_geaendert']=changed
    included=sum((amount for amount,status in zip(amounts,statuses) if status=='freigegeben'),Decimal(0))
    held=sum((amount for amount,status in zip(amounts,statuses) if status=='einbehalten'),Decimal(0))
    unknown=sum(status=='unklar' for status in statuses)
    removed=bool(set(decisions)-set(financial.abgleich_key))
    bank=core.parse_money(saved['bank']) if saved else None
    difference=included-bank if bank is not None else None
    matched=bool(saved and difference==0 and not unknown and not removed and not any(changed))
    state='abgestimmt' if matched else 'Prüfung erforderlich' if saved else 'Noch kein manueller Abgleich'
    return dict(payout=str(payout),financial=financial,children=children,bank=bank,included=included,held=held,
                difference=difference,matched=matched,status=state,active=bool(saved),unknown=unknown,
                version=document['version'],source_digest=hashlib.sha256(''.join(sorted(snapshot(r) for _,r in financial.iterrows())+sorted(snapshot(r) for _,r in children.iterrows())).encode()).hexdigest(),
                positive=sum((a for a in amounts if a>0),Decimal(0)),negative=sum((a for a in amounts if a<0),Decimal(0)))


def protected(payout, financial):
    """Conservatively lock editing of payouts with any protected business evidence."""
    import position_workflow
    with core.ledger() as db:
        state=db.execute('SELECT attempt,invoice_id FROM payouts WHERE id=?',(str(payout),)).fetchone()
        if state and any(state): return 'Lexware-Reservierung oder Übertragung vorhanden; Abgleich nur lesend.'
        for _,row in financial.iterrows():
            raw=row.copy();amount=core.parse_money(row['Betrag abzügl. Kosten'])
            raw['Art']='Erstattung' if amount<0 else 'Bestellung'
            found=db.execute('SELECT reviewed_at,paid_at,received_at,closed_at FROM position_workflow WHERE position_key=?',(position_workflow.position_key(raw),)).fetchone()
            if found and any(found): return 'Bereits bestätigte Prüfung, Zahlung oder Abschluss vorhanden; Abgleich nur lesend.'
    return ''


def save(payout, bank, decisions, actor, note, expected_version, expected_source):
    if not actor.strip() or not note.strip(): raise ValueError('Name und Beleg-/Prüfhinweis erforderlich.')
    amount=core.parse_money(bank)
    directory=Path(core.PAYOUTS_DB_PATH).parent
    with core.FileLock(core.PAYOUTS_DB_PATH+'.lock'),core.FileLock(core.ORDERS_DB_PATH+'.lock'),core.FileLock(str(directory/FILE)+'.lock'):
        document=load();current=inspect(payout,document=document)
        if document['version']!=expected_version or current['source_digest']!=expected_source:
            raise ValueError('Abgleich oder Quelldaten verändert. Ansicht aktualisieren.')
        if set(decisions)!=set(current['financial'].abgleich_key) or any(s not in STATUSES for s in decisions.values()):
            raise ValueError('Für jede Finanzbewegung ist ein gültiger Status erforderlich.')
        reason=protected(payout,current['financial'])
        if reason: raise ValueError(reason)
        previous=document['payouts'].get(str(payout))
        record=dict(bank=str(amount),items={row.abgleich_key:dict(status=decisions[row.abgleich_key],source=snapshot(row)) for _,row in current['financial'].iterrows()},
                    actor=actor.strip(),note=note.strip(),at=datetime.now(timezone.utc).isoformat())
        document['payouts'][str(payout)]=record;document['version']+=1
        document['audit'].append(dict(payout=str(payout),before=previous,after=record))
        encoded=json.dumps(document,ensure_ascii=False)
        with core.ledger() as db:
            db.execute('CREATE TABLE IF NOT EXISTS manual_payout_reconciliation (id INTEGER PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('INSERT OR REPLACE INTO manual_payout_reconciliation VALUES(1,?)',(encoded,))
            core.audit(db,payout,'Manueller Payout-Abgleich: '+actor.strip()+'; '+note.strip())
            db.commit()
        temporary=(directory/FILE).with_suffix('.json.tmp')
        with temporary.open('w',encoding='utf-8') as f:
            f.write(encoded);f.flush();os.fsync(f.fileno())
        os.replace(temporary,directory/FILE)
        return inspect(payout,document=document)


def gates(raw):
    document=load();result={}
    for payout in document['payouts']:
        if payout not in set(raw['Auszahlung Nr.']): continue
        state=inspect(payout,raw,document)
        for _,row in state['financial'].iterrows():
            status=row.Abgleichstatus
            result[key(row)]='' if status=='freigegeben' and state['matched'] else (
                'Manueller Payout-Abgleich: '+status if status!='freigegeben' else 'Manueller Payout-Abgleich: Bankbetrag noch nicht abgestimmt')
    return result
