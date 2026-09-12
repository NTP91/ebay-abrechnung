"""Explicit user correction, read-only remote verification, durable local history."""
import json
from datetime import datetime, timezone
import core
import position_workflow

RE0089_ID = 'cea421da-8ae0-46f7-8576-ba68805229a2'


def discard_re0089_test(*, confirmed_test_only=False, actor='', reason=''):
    """One explicitly authorized local test correction; no assertion of remote deletion."""
    if not confirmed_test_only or not actor.strip() or not reason.strip():
        raise ValueError('Ausdrückliche Testbeleg-Bestätigung, Name und Begründung erforderlich.')
    with core.FileLock(core.PAYOUTS_DB_PATH+'.lock'), core.FileLock(core.ORDERS_DB_PATH+'.lock'):
        master = core.load_master_data()
        with core.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = [dict(row) for row in db.execute('SELECT * FROM payouts WHERE invoice_id=? ORDER BY id', (RE0089_ID,))]
            if db.execute('SELECT 1 FROM discarded_invoices WHERE invoice_id=?', (RE0089_ID,)).fetchone():
                raise ValueError('RE0089 wurde bereits verworfen; keine erneute Änderung.')
            if not rows or any(row['attempt'] != 'created' or not row['snapshot'] for row in rows):
                raise ValueError('RE0089 nicht als eindeutiger erstellter Testentwurf vorhanden.')
            if any(row['snapshot'] != rows[0]['snapshot'] for row in rows):
                raise ValueError('Widersprüchliche RE0089-Snapshots; keine Freigabe.')
            payload = json.loads(rows[0]['snapshot'])
            if len(payload.get('lineItems', [])) != 37:
                raise ValueError('RE0089 besitzt nicht die erwarteten 37 Positionen.')
            selected = master[master['Auszahlung Nr.'].isin([row['id'] for row in rows])]
            for row in rows:
                if core.payout_fingerprint(selected[selected['Auszahlung Nr.']==row['id']]) != row['fingerprint']:
                    raise ValueError('Quelldaten seit RE0089 verändert; keine Freigabe.')
            sales = selected[(selected.Gruppe=='Gruppe B') & (selected.Art=='Bestellung') & (selected['Erlös_Brutto']>0)]
            descriptions = [f"eBay-Bestellnummer: {row['Bestellnummer']}\nSKU: {row.SKU}" for _, row in sales.iterrows()]
            if sorted(descriptions) != sorted(item.get('description','') for item in payload['lineItems']):
                raise ValueError('RE0089-Positionsumfang nicht eindeutig zugeordnet.')
            keys = set(sales.apply(position_workflow.position_key, axis=1))
            orders = set(sales.Bestellnummer)
            for record_row in db.execute('SELECT record FROM partner_invoices'):
                record = json.loads(record_row[0])
                items = record.get('expected', {}).get('items', [])
                if (any(item.get('key') in keys or item.get('order') in orders for item in items)
                        or 'RE0089' in str(record.get('invoice_number','')).upper()):
                    raise ValueError('Hochgeladener Beleg zu RE0089 vorhanden; Testkorrektur gesperrt.')
            if any(row[0] in keys for row in db.execute('SELECT position_key FROM partner_invoice_positions')):
                raise ValueError('Rechnungszuordnung vorhanden; Testkorrektur gesperrt.')
            for row in db.execute('SELECT * FROM position_workflow'):
                if row['position_key'] in keys and any(row[field] for field in position_workflow.FIELDS):
                    raise ValueError('Prüfung, Zahlung oder Abschluss vorhanden; Testkorrektur gesperrt.')
            db.execute('INSERT INTO discarded_invoices VALUES(?,?,?,?)',
                       (RE0089_ID, 'RE0089 · verworfener Testbeleg', datetime.now(timezone.utc).isoformat(), json.dumps(rows, ensure_ascii=False)))
            for row in rows:
                db.execute("UPDATE payouts SET invoice_id=NULL,attempt=NULL,fingerprint=NULL,snapshot=NULL,status='vollständig zugeordnet' WHERE id=? AND invoice_id=?", (row['id'], RE0089_ID))
                core.audit(db, row['id'], 'RE0089 als Testbeleg verworfen; ausdrücklich bestätigt durch '+actor.strip()+
                           '; '+reason.strip()+'; lokale Beleg-, Prüf-, Zahlungs- und Abschlussprüfung ohne Treffer. Keine Remote-Prüfung, Löschung oder Übertragung; API-/manuelle Holds bleiben erhalten.')
            db.commit()
    return len(keys)


def discard(api_key, invoice_id, deleted_confirmed=False, http=None):
    """Never delete remotely. Release only an existing, demonstrably deleted draft."""
    if not api_key or not deleted_confirmed:
        raise ValueError('Löschung des Entwurfs in Lexware ausdrücklich bestätigen und API-Key hinterlegen.')
    http = http or core.requests
    with core.FileLock(core.PAYOUTS_DB_PATH+'.lock'), core.FileLock(core.ORDERS_DB_PATH+'.lock'):
        business = position_workflow.positions()
        with core.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = [dict(r) for r in db.execute('SELECT * FROM payouts WHERE invoice_id=?', (invoice_id,))]
            if not rows or any(r['attempt'] != 'created' or not r['snapshot'] for r in rows):
                raise ValueError('Kein eindeutig gespeicherter Entwurf für diese Korrektur.')
            payload = json.loads(rows[0]['snapshot'])
            if any(r['snapshot'] != rows[0]['snapshot'] for r in rows):
                raise ValueError('Abweichende Entwurfsgrundlagen; manuelle Prüfung erforderlich.')
            contact_id = payload.get('address', {}).get('contactId')
            selected = business[business['Auszahlung Nr.'].isin([r['id'] for r in rows])]
            transferred = selected[selected.Lexware_uebertragen]
            if len(transferred) != len(payload.get('lineItems', [])) or transferred.empty:
                raise ValueError('Entwurfspositionen nicht mehr eindeutig zugeordnet.')
            if transferred.received_at.astype(bool).any() or transferred.closed_at.astype(bool).any():
                raise ValueError('Bereits erhaltene Zahlung oder abgeschlossene Position: Korrektur gesperrt.')
            if any(core.payout_fingerprint(selected[selected['Auszahlung Nr.']==r['id']]) != r['fingerprint'] for r in rows):
                raise ValueError('Quelldaten seit Entwurf verändert; Korrektur gesperrt.')
            headers = {'Authorization': f'Bearer {api_key}', 'Accept': 'application/json'}
            try:
                contact = http.get(core.API_URL+'/contacts/'+str(contact_id), headers=headers, timeout=20, allow_redirects=False)
                if contact.status_code != 200 or contact.json().get('id') != contact_id or str(contact.json().get('roles',{}).get('customer',{}).get('number')) != '16335':
                    raise ValueError('Evelyn-Kontakt nicht eindeutig bestätigt; Sperre bleibt bestehen.')
                response = http.get(core.API_URL+'/invoices/'+invoice_id, headers=headers, timeout=20, allow_redirects=False)
                if response.status_code != 404:
                    raise ValueError('Entwurf besteht noch oder Löschung nicht eindeutig bestätigt; Sperre bleibt bestehen.')
            except ValueError:
                raise
            except Exception:
                raise ValueError('Leseprüfung fehlgeschlagen; Sperre bleibt bestehen. Kein automatischer Wiederholungsversuch.') from None
            label = 'RE0089' if invoice_id == 'cea421da-8ae0-46f7-8576-ba68805229a2' else invoice_id
            db.execute('INSERT INTO discarded_invoices VALUES(?,?,?,?)', (invoice_id, label, datetime.now(timezone.utc).isoformat(), json.dumps(rows, ensure_ascii=False)))
            for row in rows:
                db.execute("UPDATE payouts SET invoice_id=NULL,attempt=NULL,fingerprint=NULL,snapshot=NULL,status='vollständig zugeordnet' WHERE id=? AND invoice_id=?", (row['id'], invoice_id))
                core.audit(db, row['id'], f'Entwurf {label} ({invoice_id}) verworfen: Nutzer bestätigt Löschung, Evelyn-Kontakt geprüft, Rechnung HTTP 404. Nur Transfersperre aufgehoben.')
            db.commit()
    return len(transferred)
