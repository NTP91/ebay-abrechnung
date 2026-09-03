"""Explicit user correction, read-only remote verification, durable local history."""
import json
from datetime import datetime, timezone
import core
import position_workflow


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
