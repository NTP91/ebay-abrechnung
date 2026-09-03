"""Durable incoming-invoice records mirrored independently of SQLite."""
import json
import os


def initialize(db, directory):
    db.execute('CREATE TABLE IF NOT EXISTS partner_invoices (id TEXT PRIMARY KEY, file_hash TEXT NOT NULL UNIQUE, partner TEXT NOT NULL, number_key TEXT UNIQUE, record TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS partner_invoice_positions (position_key TEXT PRIMARY KEY, invoice_id TEXT NOT NULL)')
    path = directory/'Settlement_Partner_Invoices.json'
    if path.exists():
        saved = json.loads(path.read_text(encoding='utf-8'))
        for row in saved['invoices']:
            db.execute('INSERT OR IGNORE INTO partner_invoices VALUES(?,?,?,?,?)', tuple(row[k] for k in ('id','file_hash','partner','number_key','record')))
            current = db.execute('SELECT record FROM partner_invoices WHERE id=?',(row['id'],)).fetchone()
            if current is None:
                raise ValueError('Widersprüchliche Eingangsrechnungshistorie; Wiederherstellung prüfen.')
            if current and json.loads(row['record']).get('approved_at') and not json.loads(current[0]).get('approved_at'):
                db.execute('UPDATE partner_invoices SET record=? WHERE id=?',(row['record'],row['id']))
        for row in saved['positions']:
            current = db.execute('SELECT invoice_id FROM partner_invoice_positions WHERE position_key=?',(row['position_key'],)).fetchone()
            if current and current[0] != row['invoice_id']:
                raise ValueError('Widersprüchliche Eingangsrechnungssperre; Wiederherstellung prüfen.')
            db.execute('INSERT OR IGNORE INTO partner_invoice_positions VALUES(?,?)',(row['position_key'],row['invoice_id']))


def mirror(db, directory):
    value = {'invoices':[dict(r) for r in db.execute('SELECT * FROM partner_invoices ORDER BY id')],
             'positions':[dict(r) for r in db.execute('SELECT * FROM partner_invoice_positions ORDER BY position_key')]}
    path = directory/'Settlement_Partner_Invoices.json'
    temporary = path.with_suffix('.json.tmp')
    with temporary.open('w',encoding='utf-8') as output:
        json.dump(value,output,ensure_ascii=False)
        output.flush(); os.fsync(output.fileno())
    os.replace(temporary,path)
