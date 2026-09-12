import json, os, sqlite3
from contextlib import contextmanager
from decimal import Decimal
os.environ['PAYMENT_BACKEND']='supabase'
import pandas as pd
import core, supabase_store, position_workflow, studio_view, group_b_rounds

raw,version=supabase_store.get('state/settlement.sqlite3')
db=supabase_store.sqlite_from_bytes(raw);db.row_factory=sqlite3.Row
group_b_rounds.initialize(db)
@contextmanager
def memory_ledger():
    try: yield db
    except Exception:
        db.rollback();raise
original_ledger=core.ledger;core.ledger=memory_ledger
try:
    states=pd.read_sql_query("SELECT id AS Auszahlung,status AS Status,invoice_id AS Entwurf,attempt AS Sperre FROM payouts ORDER BY id",db)
    master=core.load_master_data();business=position_workflow.positions(master,states)
    original_sync=core.sync_status;core.sync_status=lambda _:states.copy()
    try: invoices=studio_view.invoice_history()
    finally: core.sync_status=original_sync
    eligible=studio_view.eligible_rows(master,states)
    evelyn=studio_view.evelyn_overview(business,eligible,invoices)
    group_b_rounds.bootstrap(business,evelyn['new_ready'],invoices)
    view=group_b_rounds.overview(business)
    print('ROUNDS')
    for item in view['rounds']:
        print(item['round_id'],'E',item['evelyn_invoiced'],'paid',item['evelyn_paid'],'claims',item['partner_claims'],'partner paid',item['partner_paid'],'open',item['partner_open'],'reserve from Evelyn',item['reserve_from_evelyn'],'margin',item['patrick_margin'],'corr',item['corrections'],'holds',item['holds'],'funded hold',item['funded_hold_reserve'],'unfunded hold',item['unfunded_hold_reserve'])
    print('PARTNERS')
    for item in view['partners']:
        print(item)
    print('UNASSIGNED HOLDS',len(view['unassigned_holds']))
    print('EVELYN TOTAL',sum(r['evelyn_invoiced'] for r in view['rounds']))
finally:
    core.ledger=original_ledger;db.close()
print('SUPABASE HASH',__import__('hashlib').sha256(raw).hexdigest(),'VERSION',version)
