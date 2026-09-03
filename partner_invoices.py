"""Incoming partner invoices: source-derived expectations, reconciliation and proof."""
import hashlib
import json
import os
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import core
import invoice_parser
import position_workflow as workflow
from partner_export import prepare_partner_export

TOLERANCE = Decimal('0.01')


def now():
    return datetime.now(timezone.utc).isoformat()


def expected_statement(rows):
    model=prepare_partner_export(rows)
    name='Gutschriften' if (rows.Art=='Erstattung').all() else 'Rechnung'
    items=[]
    for (_,row),item in zip(rows.iterrows(),model[name]):
        items.append(dict(key=row.position_key, source=workflow.source_snapshot(row),
                          order=item['order'], sku=row.SKU, article=item['article'], quantity='1',
                          net=str(item['net']),net_after=str(item['net_after']),gross=str(item['gross']),
                          discount=str(item['discount']),rate=str((model['rate']*100).normalize()),payout=row['Auszahlung Nr.']))
    if len(items)!=len(rows):
        raise ValueError('Rechnung und Gutschrift müssen getrennt geprüft werden.')
    return dict(items=items,total=str(model['totals'][name]['gross']),partner=rows.iloc[0].Partner,scope=name)


def money(value):
    return core.parse_money(str(value).replace('%','').strip())


def reconcile(extracted, expected, all_rows, allocated):
    errors=list(extracted.get('errors',[])); warnings=list(extracted['warnings']); matched=[]; remaining=list(expected['items'])
    identities_complete=bool(extracted['items'])
    for index,item in enumerate(extracted['items'],1):
        order=item.get('order','').strip(); sku=item.get('sku','').strip()
        prefix=f'Position {index} / Bestellnummer {order or "nicht erkannt"}: '
        if order and not any(row['order']==order for row in expected['items']):
            known=all_rows[all_rows.Bestellnummer==order] if not all_rows.empty else all_rows
            errors.append(prefix+('Bestellung gehört zu einem anderen Partner.' if not known.empty and not (known.Partner==expected['partner']).any() else 'Zusätzliche/unbekannte oder nicht mehr offene Position; kein abrechenbarer Payout im erwarteten Bestand.'))
            continue
        if not order or not sku:
            warnings.append(prefix+'Bestellnummer oder SKU fehlt.'); identities_complete=False; continue
        candidates=[row for row in remaining if row['order']==order and row['sku']==sku]
        if not candidates:
            same_order=[row for row in expected['items'] if row['order']==order]
            if same_order and not any(row['sku']==sku for row in same_order):
                errors.append(prefix+'SKU stimmt nicht; erwartet '+', '.join(sorted({r['sku'] for r in same_order})))
            elif any(row['order']==order and row['sku']==sku for row in expected['items']):
                errors.append(prefix+'Rechnungsposition doppelt enthalten.')
            else:
                known=all_rows[all_rows.Bestellnummer==order] if not all_rows.empty else all_rows
                if not known.empty and not (known.Partner==expected['partner']).any():
                    errors.append(prefix+'Bestellung gehört zu einem anderen Partner: '+', '.join(sorted(known.Partner.unique())))
                else:
                    errors.append(prefix+'Zusätzliche/unbekannte oder nicht mehr offene Position; kein abrechenbarer Payout im erwarteten Bestand.')
            continue
        if len(candidates)>1:
            exact=[r for r in candidates if item.get('net') and _same_money(item['net'],r['net'])]
            if exact: candidates=exact
            elif len({r['net'] for r in candidates})>1:
                warnings.append(prefix+'Mehrere Payouttransaktionen; Betrag nicht eindeutig zuordenbar.'); identities_complete=False; continue
        wanted=candidates[0]; remaining.remove(wanted); matched.append(wanted['key'])
        if wanted['key'] in allocated:
            errors.append(prefix+'Position bereits in Rechnung '+allocated[wanted['key']]+' enthalten.')
        for field in ('article','quantity'):
            if not item.get(field): warnings.append(prefix+('Artikelname' if field=='article' else 'Menge')+' fehlt.')
        if item.get('article') and ' '.join(item['article'].split())!=' '.join(wanted['article'].split()):
            errors.append(prefix+'Artikelbezeichnung stimmt nicht.')
        if item.get('quantity'):
            try:
                if Decimal(item['quantity'].replace(',','.'))!=1: errors.append(prefix+'Menge stimmt nicht; erwartet 1.')
            except InvalidOperation: warnings.append(prefix+'Menge nicht sicher lesbar.')
        if not (item.get('net') or item.get('net_after')): warnings.append(prefix+'Netto-Positionsbetrag fehlt.')
        if not (item.get('gross') or item.get('net_after')): warnings.append(prefix+'Positionsbetrag nach Rabatt fehlt.')
        if not (item.get('rate') or item.get('discount')): warnings.append(prefix+'Rabattangabe fehlt.')
        for field in ('net','net_after','gross','discount','rate'):
            if not item.get(field): continue
            label={'net':'Netto vor Rabatt','net_after':'Netto nach Rabatt','gross':'Positionsbetrag brutto','discount':'Rabatt netto','rate':'Rabattsatz'}[field]
            try:
                rate_text=str(item[field]).replace('%','').strip().replace(',','.')
                if field=='rate' and not re.fullmatch(r'-?\d+(?:\.\d+)?',rate_text): raise ValueError('Rabatt nicht lesbar')
                actual=Decimal(rate_text) if field=='rate' else money(item[field]); target=Decimal(wanted[field])
                tolerance=Decimal(0) if field=='rate' else TOLERANCE
                if abs(actual-target)>tolerance:
                    errors.append(prefix+f'{label}: Erwartet {target}, Rechnung {actual}'+(' %' if field=='rate' else ' €'))
            except ValueError:
                warnings.append(prefix+label+' nicht sicher lesbar.')
    if identities_complete:
        for row in remaining: errors.append('Bestellnummer '+row['order']+' / SKU '+row['sku']+' fehlt auf der Rechnung.')
    elif remaining:
        warnings.append(f'{len(remaining)} erwartete Positionen konnten nicht sicher zugeordnet werden.')
    if not extracted['total']:
        warnings.append('Gesamtbetrag brutto nicht sicher erkannt.')
    else:
        try:
            actual=money(extracted['total']); target=Decimal(expected['total'])
            if abs(actual-target)>TOLERANCE: errors.append(f'Gesamtbetrag: Erwartet {target} €, Rechnung {actual} €.')
            if extracted['items'] and all(item.get('gross') for item in extracted['items']):
                line_total=sum(money(item['gross']) for item in extracted['items'])
                if abs(line_total-actual)>TOLERANCE:
                    errors.append(f'Summe der Rechnungspositionen {line_total} € stimmt nicht mit dem ausgewiesenen Gesamtbetrag {actual} € überein.')
        except ValueError: warnings.append('Gesamtbetrag nicht sicher lesbar.')
    return dict(status='deviation' if errors else 'manual_required' if warnings else 'matched',errors=errors,warnings=warnings,matched=matched)


def _same_money(a,b):
    try: return abs(money(a)-Decimal(b))<=TOLERANCE
    except ValueError: return False


def list_invoices(partner=None):
    with core.ledger() as db:
        rows=db.execute('SELECT record FROM partner_invoices ORDER BY rowid DESC').fetchall()
    records=[json.loads(row[0]) for row in rows]
    return [r for r in records if partner is None or r['partner']==partner]


def upload(partner, filename, content, scope='Rechnung'):
    suffix=Path(filename).suffix.lower()
    if suffix not in ('.pdf','.xlsx','.csv') or not content or len(content)>20*1024*1024:
        raise ValueError('PDF, XLSX oder CSV mit maximal 20 MB erforderlich.')
    digest=hashlib.sha256(content).hexdigest()
    with core.FileLock(core.PAYOUTS_DB_PATH+'.lock'),core.FileLock(core.ORDERS_DB_PATH+'.lock'):
        with core.ledger() as db:
            duplicate=db.execute('SELECT record FROM partner_invoices WHERE file_hash=?',(digest,)).fetchone()
            if duplicate: return json.loads(duplicate[0]),True
        business=workflow.positions()
        if business.empty: raise ValueError('Keine abrechenbaren Partnerpositionen vorhanden.')
        art='Erstattung' if scope=='Gutschriften' else 'Bestellung'
        rows=business[(business.Partner==partner)&(business.Art==art)&~business.closed_at.astype(bool)&~business.paid_at.astype(bool)&~business['Prüfhinweis'].astype(bool)&~business.Quellenpruefung.astype(bool)]
        if rows.empty: raise ValueError('Keine offenen abrechenbaren Positionen für diesen Partner.')
        expected=expected_statement(rows)
        extracted=invoice_parser.extract(content,filename)
        number_key=partner+'|'+invoice_parser.norm(extracted['number']) if extracted['number'] else None
        with core.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            allocated={r['position_key']:r['invoice_id'] for r in db.execute('SELECT * FROM partner_invoice_positions')}
            report=reconcile(extracted,expected,business,allocated)
            if number_key:
                same=db.execute('SELECT id FROM partner_invoices WHERE number_key=?',(number_key,)).fetchone()
                if same:
                    report['errors'].append('Rechnungsnummer für diesen Partner bereits vorhanden: '+extracted['number'])
                    report['status']='deviation'; number_key=None
            invoice_id=str(uuid.uuid4())
            directory=Path(core.PAYOUTS_DB_PATH).parent/'Partner_Invoices'; directory.mkdir(exist_ok=True)
            stored_name=digest+suffix; path=directory/stored_name
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest()!=digest: raise ValueError('Originaldatei-Hash widersprüchlich.')
            else:
                with path.open('xb') as output:
                    output.write(content);output.flush();os.fsync(output.fileno())
            record=dict(id=invoice_id,partner=partner,file_name=Path(filename).name,file_ref=stored_name,file_hash=digest,
                        uploaded_at=now(),invoice_number=extracted['number'],invoice_date=extracted['invoice_date'],
                        extracted=extracted,expected=expected,report=report,approved_at='',approved_by='',approval_mode='',override_reason='')
            db.execute('INSERT INTO partner_invoices VALUES(?,?,?,?,?)',(invoice_id,digest,partner,number_key,json.dumps(record,ensure_ascii=False)))
            core.audit(db,'',f'Eingangsrechnung {invoice_id} hochgeladen: Partner {partner}; Ergebnis {report["status"]}')
            db.commit()
        return record,False


def authorize_review(db, invoice_id, chosen, actor, reason, override_confirmed):
    """Called inside the same transaction that writes reviewed_at; no partial approval."""
    row=db.execute('SELECT record FROM partner_invoices WHERE id=?',(invoice_id,)).fetchone()
    if not row: raise ValueError('Zugehörige Eingangsrechnung fehlt; beleglose Prüfbestätigung ist gesperrt.')
    record=json.loads(row[0])
    if record['approved_at']: raise ValueError('Rechnung bereits freigegeben; keine erneute Bestätigung.')
    if not actor.strip(): raise ValueError('Name der freigebenden Person erforderlich.')
    if record['report']['status']=='deviation': raise ValueError('Rechnung weist Abweichungen auf; Freigabe gesperrt.')
    manual=record['report']['status']=='manual_required'
    if manual and (not override_confirmed or len(reason.strip())<10):
        raise ValueError('Manuelle Freigabe benötigt ausdrückliche Bestätigung und eine nachvollziehbare Begründung.')
    expected={r['key']:r for r in record['expected']['items']}
    if set(chosen.position_key)!=set(expected): raise ValueError('Positionsumfang stimmt nicht mit der Eingangsrechnung überein.')
    path=Path(core.PAYOUTS_DB_PATH).parent/'Partner_Invoices'/record['file_ref']
    if path.name!=record['file_ref'] or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=record['file_hash']:
        raise ValueError('Originalrechnung fehlt oder wurde verändert; Freigabe gesperrt.')
    for _,position in chosen.iterrows():
        if expected[position.position_key]['source']!=workflow.source_snapshot(position):
            raise ValueError('Quelldaten seit Rechnungsupload verändert; erneuten Belegabgleich durchführen.')
        prior=db.execute('SELECT invoice_id FROM partner_invoice_positions WHERE position_key=?',(position.position_key,)).fetchone()
        if prior: raise ValueError('Position bereits in Rechnung '+prior[0]+' enthalten.')
    record.update(approved_at=now(),approved_by=actor.strip(),approval_mode='manual_override' if manual else 'automatic_match',override_reason=reason.strip() if manual else '')
    db.execute('UPDATE partner_invoices SET record=? WHERE id=?',(json.dumps(record,ensure_ascii=False),invoice_id))
    for key in expected: db.execute('INSERT INTO partner_invoice_positions VALUES(?,?)',(key,invoice_id))
    core.audit(db,'',f'Eingangsrechnung {invoice_id} freigegeben: {record["approval_mode"]}; durch {actor.strip()}; Begründung {record["override_reason"]}')


def approve(invoice_id, actor, reason='', override_confirmed=False):
    record=next((r for r in list_invoices() if r['id']==invoice_id),None)
    if not record: raise ValueError('Eingangsrechnung nicht vorhanden.')
    workflow.confirm([r['key'] for r in record['expected']['items']],'review',date.today(),
                     expected_sources={r['key']:r['source'] for r in record['expected']['items']},
                     invoice_id=invoice_id,actor=actor,override_reason=reason,override_confirmed=override_confirmed)
