import streamlit as st
import hashlib
import importlib
import os
import pandas as pd
from pathlib import Path
import core
import api_holds
import ebay_sync
import data_status
import studio_view
import position_workflow
import draft_correction
import partner_invoices
import group_b_rounds
import payout_reconciliation
import trust_risk_ui
import supabase_store
from datetime import date
from partner_export import export_partner_excel, prepare_partner_export

@st.cache_resource
def _supabase_preflight():
    """One authenticated ping per process, not one per rerun/click."""
    supabase_store.preflight()
    return True


try:
    _supabase_preflight()
except supabase_store.StoreError as exc:
    st.error(f'Supabase nicht verfügbar oder nicht authentifiziert: {exc}\n\nKein lokaler Fallback möglich; Anwendung angehalten.')
    st.stop()

if not callable(getattr(studio_view,'evelyn_overview',None)):
    studio_view=importlib.reload(studio_view)
if not callable(getattr(position_workflow,'_later_hold_on_bound_invoice',None)):
    position_workflow=importlib.reload(position_workflow)


def _dashboard_cache_key():
    """Identifies which data source this run actually points at (Supabase
    namespace, or local CSV paths) - the cache must never serve one source's
    result for another's request, e.g. across tests pointed at different
    temp directories within the same process."""
    if supabase_store.enabled():
        return ('supabase', os.environ.get('PAYMENT_NAMESPACE', 'production'))
    return ('local', core.PAYOUTS_DB_PATH, core.ORDERS_DB_PATH)


def _load_dashboard_data_impl(_cache_key):
    """The full read pipeline (Supabase CSVs/ledger + reconciliation) is heavy;
    cache it for a short TTL so every click doesn't re-run it from scratch.
    Cleared explicitly wherever an action below writes new data."""
    master=core.load_master_data()
    states=core.sync_status(master)
    overview=data_status.overview(master,states)
    ready=studio_view.eligible_rows(master,states)
    business=position_workflow.positions(master,states)
    partner_ready=studio_view.partner_rows(business)
    partner_invoice_ready=partner_ready[~partner_ready.reviewed_at.astype(bool)] if not partner_ready.empty else partner_ready
    business_payout_status=position_workflow.payout_status(business)
    raw=core.read_master(core.PAYOUTS_DB_PATH)
    orders_master=core.read_master(core.ORDERS_DB_PATH)
    open_rows=studio_view.open_positions(raw,orders_master)
    catalogue=studio_view.order_catalogue(raw,business,orders_master)
    open_orders=catalogue[~catalogue.payout & (catalogue.Status!='Einbehalt / Rücksendung in Klärung')] if not catalogue.empty else catalogue
    invoices=studio_view.invoice_history()
    api_imports=ebay_sync.load(Path(core.PAYOUTS_DB_PATH).parent)
    return (master,states,overview,ready,business,partner_ready,partner_invoice_ready,
            business_payout_status,raw,orders_master,open_rows,catalogue,open_orders,invoices,api_imports)


if os.environ.get('PYTEST_CURRENT_TEST'):
    # Tests routinely seed/mutate data directly (bypassing the UI) between
    # separate AppTest runs against the same paths; a cross-run cache would
    # serve stale data there, so caching only ever runs outside pytest.
    _load_dashboard_data=_load_dashboard_data_impl
    _load_dashboard_data.clear=lambda:None
else:
    _load_dashboard_data=st.cache_data(ttl=300)(_load_dashboard_data_impl)


st.set_page_config(page_title='Payout Studio', page_icon='💠', layout='wide', initial_sidebar_state='expanded')
st.markdown('''<style>
:root{--navy:#0b2454;--ink:#15284a;--muted:#68758d;--line:#dce3ec;--surface:#fff;--page:#f7f9fc;--blue:#123f8c;--warn:#fff9e8}
html,body,[class*="css"]{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.stApp{background:linear-gradient(180deg,#fbfcfe 0,#f5f7fb 100%);color:var(--ink)}
[data-testid="stHeader"]{background:rgba(255,255,255,.94);border-bottom:1px solid #e3e8ef;backdrop-filter:blur(10px)}
[data-testid="stSidebar"]{background:#fff;border-right:1px solid #e3e8ef;box-shadow:6px 0 24px #18345b08}
.block-container{max-width:1580px;padding-top:2.5rem;padding-bottom:4rem}
h1,h2,h3{color:var(--navy);letter-spacing:-.035em;font-weight:750!important}h1{font-size:2rem!important}h3{font-size:1.32rem!important}
p{line-height:1.55}[data-testid="stCaptionContainer"]{color:var(--muted);font-size:.9rem}
[data-testid="stMetric"]{background:var(--surface);border:1px solid var(--line);border-radius:13px;padding:22px 18px;min-height:126px;display:flex;justify-content:center;box-shadow:0 8px 24px #16335b08;text-align:center}
[data-testid="stMetricLabel"]{color:#46536b;font-size:.92rem;justify-content:center}
[data-testid="stMetricValue"]{color:var(--navy);font-size:1.85rem;font-weight:750;justify-content:center}
[data-testid="stVerticalBlockBorderWrapper"]{background:var(--surface);border-color:var(--line)!important;border-radius:14px!important;box-shadow:0 8px 30px #18345b0a}
[data-testid="stDataFrame"]{border:1px solid var(--line);border-radius:11px;overflow:hidden}
[data-testid="stButton"] button,[data-testid="stDownloadButton"] button,[data-testid="stLinkButton"] a{min-height:48px;border-radius:9px!important;font-weight:650!important;border-color:var(--navy);color:var(--navy)!important;transition:all .16s ease;box-shadow:none}
[data-testid="stButton"] button:hover,[data-testid="stDownloadButton"] button:hover,[data-testid="stLinkButton"] a:hover{border-color:#164f9f;color:#164f9f!important;transform:translateY(-1px);box-shadow:0 5px 14px #173f7517}
[data-testid="stButton"] button:disabled,[data-testid="stDownloadButton"] button:disabled{color:#7c8798!important;border-color:#d8dee7!important;transform:none;box-shadow:none}
[data-testid="stButton"] [data-testid="stBaseButton-primary"],[data-testid="stDownloadButton"] [data-testid="stBaseButton-primary"]{background:var(--navy);border-color:var(--navy);color:white!important}
[data-testid="stButton"] [data-testid="stBaseButton-primary"]:hover,[data-testid="stDownloadButton"] [data-testid="stBaseButton-primary"]:hover{background:#153b79;color:white!important}
[data-testid="stButton"] [data-testid="stBaseButton-primary"]:disabled,[data-testid="stDownloadButton"] [data-testid="stBaseButton-primary"]:disabled{background:#e9edf3;border-color:#d8dee7;color:#7c8798!important;transform:none;box-shadow:none}
[data-testid="stTabs"] [role="tablist"]{gap:28px;border-bottom:1px solid #dde4ed;margin-bottom:24px}
[data-testid="stTab"],[data-testid="stTab"] p,[data-testid="stTab"] div{color:#69758b!important;font-weight:600}
[data-testid="stTab"]{padding:13px 4px;border-bottom:3px solid transparent}
[data-testid="stTab"][aria-selected="true"],[data-testid="stTab"][aria-selected="true"] p,[data-testid="stTab"][aria-selected="true"] div{color:var(--navy)!important}
[data-testid="stTab"][aria-selected="true"]{border-bottom-color:var(--navy)}
[data-testid="stFileUploaderDropzone"]{background:#f8fafd;border:1px dashed #cbd5e2;border-radius:11px}
[data-testid="stExpander"]{background:#fff;border:1px solid var(--line);border-radius:11px;box-shadow:0 3px 14px #18345b06}
[data-testid="stAlert"]{border-radius:12px;border:1px solid #e5bd46;background:var(--warn);color:#263957}
[data-testid="stCheckbox"]{padding:.1rem 0;border-bottom:1px solid #e5e9ef}
[data-testid="stCheckbox"] label{min-height:38px;color:var(--ink);font-weight:520}
hr{border-color:#e5eaf0!important}
</style>''', unsafe_allow_html=True)


def euros(value):
    return f'{value:,.2f} €'.replace(',', 'X').replace('.', ',').replace('X', '.')


def download(label, rows, key, kind='partner'):
    if rows.empty:
        return
    try:
        # Reuses the already-loaded/cached `business` snapshot instead of
        # position_workflow.positions() with no args, which re-runs the full
        # load_master_data+ledger pipeline from scratch - previously this ran
        # once per visible download button (every partner card, every
        # historical payout), the dominant cost behind slow export tabs.
        current=business
        if not current.empty:
            forbidden=set(current.loc[current.closed_at.astype(bool) | api_holds.mask(current), 'position_key'])
            rows=rows[~rows.apply(position_workflow.position_key,axis=1).isin(forbidden)]
        if rows.empty:
            st.caption('Abgeschlossen · in der Historie archiviert; kein erneuter Export.')
            return
        blob = export_partner_excel(rows, statement_type=kind)
        filename=key+('_746fd66' if key=='Partner_Patrick_MH' else '')+'.xlsx'
        st.download_button(label, blob, filename, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', key=key, icon=':material/download:', use_container_width=True)
    except ValueError as exc:
        st.warning(f'Export benötigt Prüfung: {exc}')


def stored_invoice_original(record):
    """Keep hot-reloaded app.py compatible with an older imported helper module."""
    resolver=getattr(partner_invoices,'stored_original',None)
    if callable(resolver): return resolver(record)
    path=Path(core.PAYOUTS_DB_PATH).parent/'Partner_Invoices'/record['file_ref']
    if path.name!=record['file_ref'] or not path.is_file(): return None
    return path if hashlib.sha256(path.read_bytes()).hexdigest()==record['file_hash'] else None


def invoice_history_panel(partner, rows, records, key):
    invoices=[record for record in records if record['partner']==partner and record['expected']['scope']=='Rechnung']
    if not invoices:
        return
    with st.expander(f'Rechnungshistorie · {len(invoices)}',expanded=False):
        for record in invoices:
            expected=record['expected']['items']; keys={item['key'] for item in expected}
            invoice_rows=rows[rows.position_key.isin(keys)] if not rows.empty else rows
            complete=len(invoice_rows)==len(keys)
            paid=bool(complete and invoice_rows.paid_at.astype(bool).all())
            closed=bool(complete and invoice_rows.closed_at.astype(bool).all())
            payment_dates=sorted({str(value) for value in invoice_rows.paid_at if value}) if complete else []
            payouts=sorted({item['payout'] for item in expected})
            with st.container(border=True):
                st.markdown(f"**{record['invoice_number'] or record['file_name']}** · {record['invoice_date'] or 'Datum nicht erkannt'} · **{euros(float(record['expected']['total']))}** · {len(expected)} Positionen")
                if paid:
                    st.badge('bezahlt / Partnerabrechnung abgeschlossen',icon=':material/check_circle:',color='green')
                elif record['approved_at']:
                    st.badge('geprüft/freigegeben · Zahlung offen',icon=':material/schedule:',color='orange')
                elif record['report']['status']=='matched':
                    st.badge('geprüft · Freigabe offen',icon=':material/verified:',color='blue')
                else:
                    st.badge('Prüfung offen',icon=':material/error:',color='gray')
                st.caption('Zahlungsdatum: '+(', '.join(payment_dates) if payment_dates else 'offen'))
                st.caption('Payouts: '+(', '.join(payouts) if payouts else 'nicht auf dem Beleg angegeben'))
                if paid and not closed and not invoice_rows.empty and invoice_rows.iloc[0].Gruppe=='Gruppe B':
                    st.caption('Partnerseite abgeschlossen · Evelyn-Zahlung und Gesamtabschluss bleiben davon getrennt.')
                action_col,details_col=st.columns([1,1.8],vertical_alignment='center')
                original=stored_invoice_original(record)
                with action_col:
                    if original is None:
                        st.error('Original fehlt oder Hash abweichend.')
                    else:
                        content=original if isinstance(original,bytes) else original.read_bytes()
                        st.download_button('Originalrechnung öffnen',content,record['file_name'],'application/pdf',key=key+'-'+record['id']+'-history-original',icon=':material/open_in_new:')
                with details_col:
                    with st.popover('Details',icon=':material/list_alt:'):
                        upload_stamp=studio_view.local_datetime(core.pd.Series([record['uploaded_at']])).iloc[0]
                        approval_stamp=studio_view.local_datetime(core.pd.Series([record['approved_at']])).iloc[0] if record['approved_at'] else 'offen'
                        st.caption(f'Upload: {upload_stamp} · Freigabe: {approval_stamp}')
                        detail=core.pd.DataFrame(expected)
                        st.dataframe(detail[['order','sku']].rename(columns={'order':'Bestellnummer','sku':'SKU'}),hide_index=True,use_container_width=True)


def partner_panel(rows, rate, prefix, history_rows=None):
    history_rows=rows if history_rows is None else history_rows
    if rows.empty and history_rows.empty:
        st.info('Aktuell keine offenen Partnerabrechnungen für diese Auswahl.')
        return
    invoice_records=partner_invoices.list_invoices()
    history_keys=set(history_rows.position_key) if not history_rows.empty else set()
    history_partners={record['partner'] for record in invoice_records
                      if record['expected']['scope']=='Rechnung' and history_keys.intersection(item['key'] for item in record['expected']['items'])}
    partners=sorted(set(rows.Partner) | history_partners)
    for partner in partners:
        partner_block=rows[rows.Partner==partner] if not rows.empty else rows
        partner_history=history_rows[history_rows.Partner==partner] if not history_rows.empty else history_rows
        with st.container(border=True):
            awaiting_payment=partner_block[partner_block.reviewed_at.astype(bool) & ~partner_block.paid_at.astype(bool)]
            next_invoice=partner_block[~partner_block.reviewed_at.astype(bool)]
            group_a_waits_for_all=not next_invoice.empty and not partner_history.empty and partner_history.iloc[0].Gruppe=='Gruppe A'
            st.subheader(partner)
            payout_ids=sorted(partner_block['Auszahlung Nr.'].unique())
            if payout_ids:
                st.caption(f"{len(payout_ids)} Payout{'s' if len(payout_ids)!=1 else ''} im noch nicht abgeschlossenen Partnerbestand. Jede Statusstufe wird separat ausgewiesen.")
            else:
                st.caption('Aktuell keine neue oder zahlungsoffene Partnerposition. Abgeschlossene Rechnungen bleiben hier auffindbar.')
            payment_col,new_col=st.columns(2)
            with payment_col:
                with st.container(border=True):
                    st.markdown('**Freigegebene Rechnung · Zahlung offen**')
                    covered=set()
                    for record in invoice_records:
                        if record['partner']!=partner:
                            continue
                        if not record['approved_at'] or record['expected']['scope']!='Rechnung':
                            continue
                        invoice_keys={item['key'] for item in record['expected']['items']}
                        invoice_rows=awaiting_payment[awaiting_payment.position_key.isin(invoice_keys)]
                        if invoice_rows.empty:
                            continue
                        covered.update(invoice_rows.position_key)
                        st.metric('In freigegebener Rechnung',f'{len(invoice_rows)} Positionen')
                        st.metric('Zahlung offen',euros(partner_invoices.confirmed_payment_total(invoice_rows)))
                        st.caption(f"{record['invoice_number'] or record['file_name']} · geprüft/freigegeben · Partnerzahlung noch offen")
                        label='Bezahlt / abgeschlossen' if invoice_rows.iloc[0].Gruppe=='Gruppe A' else 'Partner bezahlt'
                        if group_a_waits_for_all:
                            st.caption('Zahlungsabschluss erst möglich, wenn alle aktuell offenen Positionen dieses Partners geprüft sind.')
                        elif st.button(label,key=prefix+'_'+partner+'-'+record['id']+'-partner-paid',icon=':material/check_circle:',use_container_width=True):
                            st.session_state['confirmation_request']=(invoice_rows.copy(),'partner_paid',label)
                    if awaiting_payment.empty:
                        st.caption('Aktuell wartet keine freigegebene Partnerrechnung auf Zahlung.')
                    elif set(awaiting_payment.position_key)-covered:
                        st.warning('Geprüfte Positionen ohne eindeutigen freigegebenen Rechnungsbezug; Zahlung gesperrt.')
            with new_col:
                with st.container(border=True):
                    st.markdown('**Neu für nächste Rechnung**')
                    if next_invoice.empty:
                        st.caption('Keine neuen abrechnungsfähigen Positionen.')
                    else:
                        try:
                            pending=studio_view.partner_summary(next_invoice).iloc[0]
                        except ValueError as exc:
                            st.warning('Neue Abrechnung benötigt Prüfung: '+str(exc))
                        else:
                            next_sales=next_invoice[next_invoice.Art=='Bestellung']
                            next_refunds=next_invoice[next_invoice.Art=='Erstattung']
                            st.metric('Neu für nächste Rechnung',f'{len(next_sales)} Positionen')
                            if not next_refunds.empty:
                                st.metric('Refunds / Abzüge',f"{euros(pending['Refunds'])} · {len(next_refunds)} Erstattung(en)")
                            st.metric('Verbleibender Partneranspruch',euros(pending['Verbleibender Anspruch']))
                            st.caption('Noch nicht geprüft/freigegeben · nicht in einer bestehenden Partnerrechnung gebunden. Refunds sind einzeln im Blatt „Erstattungen / Abzüge“ der Abrechnung nachvollziehbar.')
                            if group_a_waits_for_all and awaiting_payment.empty:
                                st.caption('Zahlungsabschluss erst möglich, wenn alle aktuell offenen Positionen dieses Partners geprüft sind.')
                            download('Einzelabrechnung herunterladen',next_invoice,prefix+'_'+partner)
            refund_cases=studio_view.partner_refund_cases(partner_history)
            if not refund_cases.empty:
                with st.expander(f"Rückzahlungs-/Gutschriftfälle · {len(refund_cases)} bereits geprüfte/bezahlte Positionen mit späterer Erstattung"):
                    st.caption('Bereits geprüfte oder bezahlte Positionen bleiben unverändert; hier nur zur separaten Klärung mit dem Partner.')
                    st.dataframe(refund_cases[['Auszahlung Nr.','Bestellnummer','Partner','SKU','Erlös_Brutto']],hide_index=True,use_container_width=True)
            st.caption(f'Neu abrechnungsfähig: Rabatt {rate} wird auf den Nettobetrag berechnet. Bezahlte Belege bleiben hier in der Rechnungshistorie und zusätzlich unter Historie → Positionsstatus sichtbar.')
            invoice_history_panel(partner,partner_history,invoice_records,prefix+'_'+partner)
            invoice_panel(next_invoice,prefix+'_'+partner)


def invoice_report(record, key, allow_approval=True):
    status=record['report']['status']
    title=record['invoice_number'] or record['file_name']
    stamp=studio_view.local_datetime(core.pd.Series([record['uploaded_at']])).iloc[0]
    st.write(f"**{title}** · {record['partner']} · hochgeladen {stamp}")
    if record['approved_at']:
        mode='manuelle Freigabe' if record['approval_mode']=='manual_override' else 'automatischer Abgleich erfolgreich'
        st.success(f"Freigegeben: {mode} · {record['approved_by']}")
        st.caption('Freigabe: '+studio_view.local_datetime(core.pd.Series([record['approved_at']])).iloc[0])
        if record['override_reason']: st.caption('Begründung: '+record['override_reason'])
    elif status=='matched': st.success('Vollständig geprüft · alle Positionen und Beträge stimmen überein.')
    elif status=='deviation': st.error('Abweichung · keine Freigabe möglich.')
    else: st.warning('Manuelle Prüfung erforderlich · der Beleg konnte nicht vollständig sicher ausgelesen werden.')
    for message in record['report']['errors']: st.error(message)
    for message in record['report']['warnings']: st.warning(message)
    with st.expander('Rechnungsdetails und erkannte Positionen'):
        st.caption(f"Rechnungsdatum: {record['invoice_date'] or 'nicht erkannt'} · Erwartet: {len(record['expected']['items'])} Positionen · {euros(float(record['expected']['total']))} brutto")
        st.dataframe(core.pd.DataFrame(record['extracted']['items']).rename(columns={'order':'Bestellnummer','sku':'SKU','article':'Artikel','quantity':'Menge','net':'Netto vor Rabatt','net_after':'Netto nach Rabatt','gross':'Positionsbetrag brutto','rate':'Rabatt %','discount':'Rabatt netto'}),hide_index=True,use_container_width=True)
        st.caption('Zugehöriger Sollbestand zum Upload-Zeitpunkt')
        expected_table=core.pd.DataFrame(record['expected']['items'])
        st.dataframe(expected_table[['order','sku','article','quantity','net','gross','rate','payout']].rename(columns={'order':'Bestellnummer','sku':'SKU','article':'Artikel','quantity':'Menge','net':'Netto vor Rabatt','gross':'Positionsbetrag brutto','rate':'Rabatt %','payout':'Payoutnummer'}),hide_index=True,use_container_width=True)
        stored=stored_invoice_original(record)
        if stored is not None:
            content=stored if isinstance(stored,bytes) else stored.read_bytes()
            st.download_button('Originalrechnung herunterladen',content,record['file_name'],key=key+'-original',icon=':material/download:')
    if allow_approval and not record['approved_at'] and status!='deviation':
        actor=st.text_input('Freigebende Person',value='Patrick',key=key+'-actor')
        reason=''; override=False
        if status=='manual_required':
            reason=st.text_area('Begründung der manuellen Freigabe',key=key+'-reason')
            override=st.checkbox('Ich habe die Originalrechnung vollständig gegen den angezeigten Sollbestand geprüft und bestätige die manuelle Freigabe ausdrücklich.',key=key+'-override')
        if st.button('Manuelle Freigabe speichern' if status=='manual_required' else 'Geprüfte Rechnung freigeben',key=key+'-approve',disabled=not actor.strip() or (status=='manual_required' and (not override or len(reason.strip())<10)),type='primary'):
            try:
                partner_invoices.approve(record['id'],actor,reason,override)
                _load_dashboard_data.clear()
                st.rerun()
            except ValueError as exc: st.error(str(exc))


def invoice_panel(rows, key, scope='Rechnung', expanded=False, choose_partner=False):
    if rows.empty: return
    partner=rows.iloc[0].Partner
    with st.expander('Partnerrechnung hochladen und prüfen', expanded=expanded):
        if choose_partner:
            available_partners=sorted(rows.Partner.unique())
            selected=st.selectbox('Partner / Händler',available_partners,index=None,placeholder='Partner auswählen',key=key+'-invoice-partner')
            if selected is None:
                st.info('Bitte zuerst den Partner auswählen. Die Prüfung entspricht dem Upload in seiner Partnerkarte.')
                return
        else:
            selected=partner
            st.caption('Partner / Händler: '+selected)
        st.caption('PDF, XLSX oder CSV · höchstens 20 MB. Strukturierte Bestellnummern, SKU, Artikel, Menge, Beträge, Rabatt und Gesamtsumme werden geprüft. Unlesbare Felder bleiben prüfpflichtig.')
        uploaded=st.file_uploader('Eingehende Partnerrechnung',type=['pdf','xlsx','csv'],key=key+'-invoice-file')
        if st.button('Rechnung hochladen und abgleichen',disabled=uploaded is None,key=key+'-invoice-upload'):
            try:
                record,duplicate=partner_invoices.upload(selected,uploaded.name,uploaded.getvalue(),scope)
                _load_dashboard_data.clear()
                if duplicate: st.info('Datei bereits vorhanden. Keine erneute Verarbeitung oder Freigabe. Zugeordnet zu '+record['partner']+'.')
            except ValueError as exc: st.error(str(exc))
        for record in partner_invoices.list_invoices(selected):
            if record['expected']['scope']==scope:
                invoice_report(record,key+'-'+record['id'])


@st.dialog('Vorgang bestätigen')
def confirm_dialog(rows, action, label, invoice_id=None):
    st.write(f"**{label}** · {len(rows)} Positionen")
    st.write('Partner: '+', '.join(sorted(rows.Partner.unique())))
    st.write('Payouts: '+', '.join(sorted(rows['Auszahlung Nr.'].unique())))
    if action in ('partner_paid','refund_settled'):
        total=partner_invoices.confirmed_payment_total(rows)
    else:
        model=prepare_partner_export(rows,statement_type='group_b_evelyn' if action=='evelyn_received' else 'partner')
        total=sum(t['gross'] for t in model['totals'].values())
    st.write('Abrechnungsbetrag brutto: '+euros(total))
    st.caption('Datum: '+date.today().strftime('%d.%m.%Y')+'. Nur den tatsächlich geprüften bzw. bezahlten Vorgang bestätigen.')
    st.dataframe(rows[['Bestellnummer','SKU','Angebotstitel','Erlös_Brutto']], hide_index=True)
    if st.button('Verbindlich bestätigen', type='primary'):
        try:
            position_workflow.confirm(rows.position_key.tolist(), action, date.today(), invoice_id=invoice_id,
                expected_sources={r.position_key:position_workflow.source_snapshot(r) for _,r in rows.iterrows()})
            _load_dashboard_data.clear()
            st.session_state.pop('confirmation_request',None)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    if st.button('Abbrechen',key='cancel-confirmation'):
        st.session_state.pop('confirmation_request',None)
        st.rerun()


def checkbox_confirmation(key, rows, action, label, invoice_id=None):
    if st.session_state.get(key):
        st.session_state['confirmation_request']=(rows.copy(),action,label,invoice_id)
        st.session_state[key]=False


@st.dialog('Lexware-Entwurf verwerfen')
def discard_dialog(invoice_id):
    st.write('Den Entwurf zuerst in Lexware löschen. Danach prüft die App ausschließlich lesend, ob er nicht mehr vorhanden ist, und gibt seine Transfersperre frei. Partnerstatus und Historie bleiben erhalten.')
    st.link_button('Entwurf in Lexware öffnen', 'https://app.lexware.de/permalink/invoices/view/'+invoice_id)
    if st.button('Entwurf wurde in Lexware gelöscht – prüfen und freigeben', disabled=not api_key, type='primary'):
        try:
            draft_correction.discard(api_key, invoice_id, deleted_confirmed=True)
            _load_dashboard_data.clear()
            st.session_state.pop('discard_request',None)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    if not api_key:
        st.caption('Für die Leseprüfung den API-Key unter Lexware-Verbindung hinterlegen.')
    if st.button('Abbrechen',key='cancel-discard'):
        st.session_state.pop('discard_request',None)
        st.rerun()


def workflow_panel(rows, key, mode='partner', action_only=False):
    if rows.empty:
        return
    active=rows[~rows['closed_at'].astype(bool) & ~rows['Prüfhinweis'].astype(bool) & ~rows.Quellenpruefung.astype(bool) & ~api_holds.mask(rows)]
    if mode=='evelyn':
        invoice_map=dict(zip(states.Auszahlung,states.Entwurf))
        active=active[~active.received_at.astype(bool)].copy()
        active['invoice_scope']=active['Auszahlung Nr.'].map(invoice_map)
        groups=active.groupby('invoice_scope')
    else:
        groups=[(rows.iloc[0].Partner,active)]
    for scope, block in groups:
        if mode=='evelyn':
            label='Zahlung von Evelyn erhalten'
            actions=[(label,'evelyn_received')]
            if not action_only:
                st.caption(f'Entwurf · {len(block)} Positionen · Payouts '+', '.join(sorted(block['Auszahlung Nr.'].unique())))
        else:
            unreviewed=block[~block.reviewed_at.astype(bool)]
            reviewed=unreviewed.empty
            open_label='offene Position' if len(block)==1 else 'offene Positionen'
            review_label='Position noch nicht geprüft' if len(unreviewed)==1 else 'Positionen noch nicht geprüft'
            if not action_only:
                st.caption(f"{len(block)} {open_label} · "+('Partnerrechnung geprüft' if reviewed else f'{len(unreviewed)} {review_label}'))
            if not reviewed:
                st.caption('Freigabe erst nach Upload und erfolgreicher Prüfung der Partnerrechnung.')
                if not block.empty and block.iloc[0].Gruppe=='Gruppe A':
                    st.caption('Zahlungsabschluss erst möglich, wenn alle aktuell offenen Positionen dieses Partners geprüft sind.')
                actions=[]
            elif mode=='refund':
                actions=[('Erstattung erledigt bestätigen','refund_settled')]
            else:
                block=block[~block.paid_at.astype(bool)]
                actions=[('Bezahlt / abgeschlossen' if block.empty or block.iloc[0].Gruppe=='Gruppe A' else 'Partner bezahlt','partner_paid')]
        if block.empty:
            continue
        for label, action in actions:
            if st.button(label, key=key+str(scope)+action, icon=':material/check_circle:', use_container_width=True):
                st.session_state['confirmation_request']=(block.copy(),action,label)


with st.sidebar:
    st.markdown('### 💠 Payout Studio')
    st.caption('RECOVERY · PATRICKS ARBEITSBEREICH')
    st.divider()
    st.subheader('Import & Dateien')
    with st.expander('eBay-Berichte hochladen', expanded=True):
        payouts = st.file_uploader('Transaktionsberichte', type=['csv'], accept_multiple_files=True)
        orders = st.file_uploader('Bestellberichte', type=['csv','xlsx'], accept_multiple_files=True)
        if st.button('Dateien importieren', type='primary', disabled=not(payouts or orders), use_container_width=True):
            try:
                receipts=[]
                for kind, files in [('orders',orders),('payout',payouts)]:
                    for uploaded in files:
                        receipts.append(data_status.import_file(uploaded,kind))
                _load_dashboard_data.clear()
                st.session_state['import_receipts']=receipts
            except Exception as exc:
                st.error(f'Import angehalten: {exc}')
        if st.session_state.get('import_receipts'):
            with st.expander('Letzter Import', expanded=True):
                for receipt in st.session_state['import_receipts']:
                    st.write(receipt['filename'])
                    if receipt['error']:
                        st.error(receipt['error'])
                    elif receipt.get('transactions'):
                        c=receipt['transactions']
                        st.caption(f"{c['new_paid']} neu ausgezahlt · {c['known_paid']} bereits vorhanden")
                        st.caption(f"{c['new_open']} neu offen · {c['still_open']} weiterhin offen · {c['assigned_open']} jetzt einem Payout zugeordnet")
                        for p in receipt['payouts']:
                            if p.get('warning'):
                                st.warning(f"{p['number']}: {p['warning']}")
                            elif p['known'] and not p.get('counts',{}).get('new_paid'):
                                st.caption(f"{p['number']}: bereits vorhanden / keine neuen Daten"+(' · Lexware-Übertragung gesperrt' if p.get('locked') else ''))
                        if receipt['issues']:
                            st.warning(f"{receipt['issues']} Zuordnungen prüfen")
                    else:
                        st.caption(f"{receipt['added']} neu · {receipt['present']} bereits vorhanden · {receipt['issues']} unvollständig")
                        if receipt.get('historical_without_sku'):
                            st.caption(f"{receipt['historical_without_sku']} historische Positionen ohne SKU archiviert · nicht abrechnungsrelevant")
    invoice_entry = st.empty()
    st.divider()
    with st.expander('Lexware-Verbindung'):
        api_key=st.text_input('API-Key',type='password')
        st.caption('Evelyn · Kundennummer 16335. Nur Entwürfe, kein Versand.')
    with st.expander('Datensicherung'):
        st.caption('Quelldaten, Historie und Rechnungssperren gemeinsam sichern.')
        try:
            if Path(core.PAYOUTS_DB_PATH).exists():
                st.download_button('Vollständiges Backup',core.backup_data(),'Settlement_Backup.zip','application/zip')
        except Exception as exc:
            st.error(str(exc))

st.title('Payout Studio')
st.caption('Dein Payout- & Abrechnungstool für eBay')
with st.expander('So läuft die Wochenabrechnung'):
    st.markdown("""1. Unter der Woche neue Payouts und Bestellberichte importieren.
2. Die App gleicht Bestellungen und Payouts automatisch ab.
3. Noch nicht ausgezahlte Bestellungen bleiben offen.
4. Am wöchentlichen Abrechnungstag Gruppe A und Gruppe B prüfen.
5. Abrechnungsübersichten erstellen und bei Bedarf einen Lexware-Entwurf erzeugen.
6. Partnerrechnungen gegen die konkrete Übersicht prüfen.
7. Zahlungseingang und Partnerzahlungen bestätigen.
8. Erledigte Positionen werden abgeschlossen und bleiben in der Historie.
9. In der nächsten Woche nur neue und noch offene Positionen bearbeiten.""")
    st.caption('Partnerrechnung hochladen und automatisch abgleichen. Uneindeutige Belege benötigen eine dokumentierte manuelle Prüfung.')

try:
    (master,states,overview,ready,business,partner_ready,partner_invoice_ready,
     business_payout_status,raw,orders_master,open_rows,catalogue,open_orders,invoices,api_imports)=_load_dashboard_data(_dashboard_cache_key())
except Exception as exc:
    st.error(f'Datenbestand benötigt Prüfung: {exc}')
    st.stop()
def close_incoming_invoice():
    st.session_state.pop('incoming_invoice_open',None)


@st.dialog('Händlerrechnung hochladen', width='large', on_dismiss=close_incoming_invoice)
def incoming_invoice_dialog():
    st.caption('Partner auswählen und die Rechnung gegen die bestehende Einzelabrechnung prüfen.')
    if partner_invoice_ready.empty:
        st.info('Aktuell keine neuen abrechnungsfähigen Partnerpositionen. Freigegebene Belege findest du beim Partner und unter Historie → Eingangsrechnungen.')
    else:
        invoice_panel(partner_invoice_ready, 'import-invoice', expanded=True, choose_partner=True)
    if st.button('Schließen',key='close-incoming-invoice'):
        close_incoming_invoice()
        st.rerun()

with invoice_entry.container():
    if st.button('Händlerrechnung hochladen', key='open-incoming-invoice', icon=':material/upload_file:', use_container_width=True):
        st.session_state['incoming_invoice_open']=True
    st.caption('Alternativer Einstieg zur Partnerkarte · Partner auswählen, dieselbe Rechnung hochladen und prüfen.')

with st.sidebar:
    def api_time(value):
        return studio_view.local_datetime(core.pd.Series([value])).iloc[0] if value else 'noch kein Abruf'
    automatic=[r for r in api_imports['runs'] if r['trigger']=='automatic' and r['status']=='success']
    manual=[r for r in api_imports['runs'] if r['trigger']=='manual']
    st.caption('Datenstand der eBay-API: '+api_time(api_imports['watermark']))
    st.caption('Letzter erfolgreicher automatischer Abruf: '+api_time(automatic[-1]['at'] if automatic else None))
    st.caption('Letzter manueller Abruf: '+api_time(manual[-1]['at'] if manual else None))
    if api_imports['runs'] and api_imports['runs'][-1]['status']!='success':
        st.warning('Letzter API-Import: '+api_imports['runs'][-1]['status']+'. Details in der Historie.')

if st.session_state.pop('draft_created',False):
    st.success('Lexware-Entwurf erstellt. Die enthaltenen Positionen sind jetzt dauerhaft gesperrt.')

with st.expander('Payout-Abgleich · Bankbetrag und einzelne Positionen'):
    payout_ids=sorted(set(raw['Auszahlung Nr.'])-{''},reverse=True)
    if not payout_ids:
        st.info('Noch keine Payouts vorhanden.')
    else:
        manual_pid=st.selectbox('Payout für Bankabgleich',payout_ids,key='manual-payout')
        try:
            check=payout_reconciliation.inspect(manual_pid,raw)
            locked=payout_reconciliation.protected(manual_pid,check['financial'])
            st.caption('Freigegeben bedeutet: diese Bewegung wird im Bankabgleich berücksichtigt. Negative Einbehalte/Gebühren werden mit ihrem Vorzeichen berücksichtigt und bleiben außerhalb der Partnerrechnung. Child-Zeilen werden nicht zusätzlich summiert.')
            if locked: st.info(locked)
            st.caption('Ohne gespeicherten manuellen Abgleich gilt die automatische Positionsprüfung. Unklare Zeilen, Holds und Erstattungen bleiben einzeln gesperrt; saubere Positionen eines finalen Payouts können weiterlaufen.')
            revision=f"manual-{manual_pid}-{check['version']}-{check['source_digest']}"
            api_control=api_imports['payouts'].get(manual_pid)
            if api_control:
                st.caption('Maßgeblicher API-Kontrollbetrag: '+euros(float(api_control['amount']['value'])))
            bank_default=check['bank'] if check['bank'] is not None else ebay_sync.eur(api_control['amount']) if api_control else None
            bank=st.text_input('Tatsächlicher Bank-/Auszahlungsbetrag in Euro',value=str(bank_default).replace('.',',') if bank_default is not None else '',key=revision+'-bank',disabled=bool(locked))
            financial=check['financial']
            editor=financial[['abgleich_key','Typ','Bestellnummer','Transaktionsnummer','SKU','Angebotstitel','Betrag abzügl. Kosten','Abgleichstatus','Quelle_geaendert']].copy()
            edited=st.data_editor(editor,hide_index=True,use_container_width=True,key=revision+'-rows',
                disabled=True if locked else [c for c in editor.columns if c!='Abgleichstatus'],
                column_config={'abgleich_key':None,'Abgleichstatus':st.column_config.SelectboxColumn('Status',options=list(payout_reconciliation.STATUSES),required=True)})
            preview=sum((core.parse_money(r['Betrag abzügl. Kosten']) for _,r in edited.iterrows() if r.Abgleichstatus=='freigegeben'),core.Decimal(0))
            col1,col2=st.columns(2)
            col1.metric('Berücksichtigte Bewegungen',euros(preview))
            if bank:
                try: col2.metric('Differenz zum Bankbetrag',euros(preview-core.parse_money(bank)))
                except ValueError: st.warning('Bankbetrag bitte als gültigen Eurobetrag eingeben.')
            st.write('Gespeicherter Abgleich: **'+check['status']+'**')
            st.caption(f"{sum(financial.Abgleichstatus=='freigegeben')} freigegebene Bewegungen · {sum(financial.Abgleichstatus=='einbehalten')} einbehalten · {check['unknown']} unklar. Differenz = berücksichtigte Bewegungen minus Bankbetrag.")
            if not check['children'].empty:
                st.caption('Zugehörige Artikelreferenzen: Status folgt der Parent-Zeile; keine eigene Finanzbuchung.')
                st.dataframe(check['children'][['Bestellnummer','Transaktionsnummer','SKU','Angebotstitel','Zwischensumme Artikel']],hide_index=True,use_container_width=True)
            actor=st.text_input('Prüfende Person',key=revision+'-actor',disabled=bool(locked))
            note=st.text_input('Beleg / Begründung des Abgleichs',key=revision+'-note',disabled=bool(locked))
            if st.button('Payout-Abgleich speichern',type='primary',disabled=bool(locked) or not bank or not actor.strip() or not note.strip(),key=revision+'-save'):
                try:
                    payout_reconciliation.save(manual_pid,bank,dict(zip(edited.abgleich_key,edited.Abgleichstatus)),actor,note,check['version'],check['source_digest'])
                    _load_dashboard_data.clear()
                    st.rerun()
                except ValueError as exc: st.error(str(exc))
            document=payout_reconciliation.load()
            events=[e for e in document['audit'] if e['payout']==manual_pid]
            if events:
                with st.expander('Bisherige manuelle Abgleiche'):
                    for event in reversed(events):
                        saved=event['after']
                        stamp=studio_view.local_datetime(core.pd.Series([saved['at']])).iloc[0]
                        st.write(f"{stamp} · {saved['actor']} · Bankbetrag {euros(core.parse_money(saved['bank']))}")
                        st.caption(saved['note'])
        except ValueError as exc:
            st.error(str(exc))

home, group_a, group_b, pending, history, dashboard, trust_risk_tab = st.tabs(['Übersicht','Gruppe A','Gruppe B','Offene Positionen','Historie','Dashboard','Trust / Risk'])
with home:
    total=len(catalogue)
    assigned=int(catalogue.payout.sum())
    without=total-assigned
    completed=int(catalogue.closed.sum())
    issue_count=int(master['Prüfhinweis'].astype(bool).sum()) if not master.empty else 0
    for col,label,value in zip(st.columns(6),['Bestellpositionen gesamt','eBay-Payout vorhanden','Noch ohne Payout','Ausgezahlt, noch nicht abgeschlossen','Abgeschlossen','Prüfpositionen'],[total,assigned,without,assigned-completed,completed,issue_count]):
        col.metric(label,value)
    st.progress(assigned/total if total else 0,text=f'{assigned} von {total} Bestellpositionen einem eBay-Payout zugeordnet · {without} noch ohne Payout')
    st.caption('Bestellberichte und Bestelltransaktionen ohne Doppelzählung. „Ohne Payout“ bedeutet: im vorhandenen Datenbestand kein Payout bekannt. Ein Lexware-Entwurf ist keine Zahlung.')
    if issue_count:
        st.warning(f'{issue_count} Zuordnungen prüfen. Nur die betroffenen Positionen bleiben gesperrt.')
    if not overview['warnings'].empty:
        st.warning('Importhinweise vorhanden. Details findest du in der Historie.')
    for gap in overview['gaps']:
        st.warning(gap)
    left,right=st.columns([1.15,1])
    with left,st.container(border=True):
        st.subheader('Datenstand')
        latest=overview['latest']
        st.write('**Letzter bekannter Payout**')
        st.write(latest['Payoutnummer']+' · '+latest['Datum / Zeitraum'] if latest else 'Noch keine Payouts importiert')
        st.caption('Bestelldaten vorhanden bis: '+(overview['order_end'] or 'noch nicht bekannt'))
        st.caption(f"{len(states)} Payouts im Bestand · {sum(s != 'abgeschlossen' for s in business_payout_status.values())} noch nicht abgeschlossen")
    with right,st.container(border=True):
        st.subheader('Nächster Schritt')
        st.write('**Zuordnungen prüfen**' if issue_count else '**Abrechnungen prüfen**' if not partner_ready.empty else '**Neue Berichte importieren**')
        st.caption('Partnerübersichten findest du in Gruppe A und Gruppe B. Erstattungen und Transaktionen ohne Payout stehen unter Offene Positionen.')
        st.write(f'{len(open_orders)} Bestellpositionen ohne bekannten Payout.')

with group_a:
    st.subheader('Gruppe A · Direktabrechnungen')
    st.caption('Rechnung hochladen → automatisch prüfen → freigeben. Danach erscheint „Bezahlt / abgeschlossen“.')
    st.caption('PP · BA · MK · 001 — 0,5 % Rabatt. Unabhängig von Patrick → Evelyn. Abschluss erst nach Prüfung und bestätigter Partnerzahlung.')
    with st.container(border=True):
        partner_panel(partner_ready[partner_ready.Gruppe=='Gruppe A'] if not partner_ready.empty else partner_ready,'0,5 %','Gruppe_A',business[business.Gruppe=='Gruppe A'] if not business.empty else business)

with group_b:
    b_ready=ready[ready.Gruppe=='Gruppe B'] if not ready.empty else ready
    evelyn=studio_view.evelyn_overview(business,b_ready,invoices)
    if not {'new_ready','new_review','new_held','prior_held'}.issubset(evelyn):
        studio_view=importlib.reload(studio_view)
        invoices=studio_view.invoice_history()
        evelyn=studio_view.evelyn_overview(business,b_ready,invoices)
    has_re0090=any(item.get('Belegnummer')=='RE0090' and not item.get('discarded') for item in invoices.values())
    if has_re0090:
        group_b_rounds.bootstrap(business,evelyn['new_ready'],invoices)
    round_view=group_b_rounds.overview(business)
    if round_view['rounds']:
      with st.container(border=True):
        st.subheader('Gruppe-B-Abrechnungsrunden')
        st.caption('Evelyn-Belege, Partneransprüche und spätere Korrekturen bleiben dauerhaft getrennten Ursprungsrunden zugeordnet.')
        for settlement in round_view['rounds']:
            parts=[]
            evelyn_status=('Evelyn bezahlt' if settlement['evelyn_paid'] else
                           'Evelyn offen · '+euros(settlement['evelyn_invoiced']))
            for partner in settlement['partners']:
                if partner['paid'] and partner['open'] <= 0:
                    label=f"{partner['partner']} {euros(partner['paid'])} bezahlt"
                else:
                    label=f"{partner['partner']} {euros(max(partner['open'],0))} offen"
                if partner['prior_corrections']:
                    label+=f" · Alt-Korrektur {euros(partner['prior_corrections'])}"
                parts.append(label)
            st.markdown(f"**{settlement['round_id']}** | {evelyn_status} | "+' | '.join(parts))
            st.caption('Partneransprüche '+euros(settlement['partner_claims'])+
                       ' · Partner bezahlt '+euros(settlement['partner_paid'])+
                       ' · Partner offen/reserviert '+euros(settlement['partner_open'])+
                       ' · aus Evelyn-Zahlung reserviert '+euros(settlement['reserve_from_evelyn'])+
                       ' · Patrick-Marge rechnerisch '+euros(settlement['patrick_margin'])+
                       ' · Korrekturen '+euros(settlement['corrections'])+
                       f" · Holds {settlement['holds']} / bezahlt gedeckt "+euros(settlement['funded_hold_reserve'])+
                       ' / noch nicht fakturiert '+euros(settlement['unfunded_hold_reserve']))
        if not round_view['unassigned_holds'].empty:
            st.warning(f"{len(round_view['unassigned_holds'])} ungeklärte Hold-/Korrekturpositionen sind keiner abgeschlossenen Runde zugeschlagen und keine freie Patrick-Marge.")
        with st.expander('Gesamtsicht je Partner',expanded=False):
            partner_totals=[]
            for partner, rows_for_partner in pd.DataFrame(round_view['partners']).groupby('partner') if round_view['partners'] else []:
                partner_totals.append({'Partner':partner,
                    'Positiver Anspruch':sum(rows_for_partner.positive),
                    'Refunds/Korrekturen':sum(rows_for_partner.corrections),
                    'Bereits bezahlt':sum(rows_for_partner.paid),
                    'Noch offen':sum(rows_for_partner.open),
                    'Aktuell zahlbar':sum(rows_for_partner.open)})
            if partner_totals:
                st.dataframe(pd.DataFrame(partner_totals),hide_index=True,use_container_width=True)
    transferred_rows=evelyn['bound']
    with st.container(border=True):
        st.subheader('Partner → Patrick')
        st.caption('Einzelabrechnungen für MH, NB und weitere zugeordnete Partner · 3,5 % Rabatt')
        partner_panel(partner_ready[partner_ready.Gruppe=='Gruppe B'] if not partner_ready.empty else partner_ready,'3,5 %','Partner_Patrick',business[business.Gruppe=='Gruppe B'] if not business.empty else business)
    with st.container(border=True):
        st.subheader('Gesamtabrechnung Gruppe B an Evelyn')
        active_invoices=[item for item in invoices.values() if not item['discarded']]
        transmitted=len(evelyn['bound'])

        st.markdown('**Neu für nächste Rechnung**')
        chosen=evelyn['new_ready']
        available=sorted(chosen['Auszahlung Nr.'].unique()) if not chosen.empty else []
        selected=available
        totals=None
        if not chosen.empty:
            try:
                totals=prepare_partner_export(chosen,statement_type='group_b_evelyn')['totals']['Rechnung']
            except ValueError as exc:
                st.warning(str(exc))
        visible_totals=totals or {'ebay':0,'discount':0,'gross':0}
        for col,label,value in zip(st.columns(4),['Neu für Evelyn','Neuer eBay-Auszahlungsbetrag brutto','Neuer Rabatt 0,5 % netto','Neue Rechnungssumme brutto'],[str(len(chosen)),euros(visible_totals['ebay']),euros(visible_totals['discount']),euros(visible_totals['gross'])]):
            col.metric(label,value)
        if totals:
            st.caption('Diese Summen enthalten ausschließlich Positionen des nächsten Lexware-Entwurfs.')
        else:
            st.info('Aktuell keine neuen Gruppe-B-Positionen für einen Lexware-Entwurf freigegeben.')

        can_create=studio_view.lexware_create_ready(
            selected, totals, api_key,
            (st.session_state.get('lexware-received'), st.session_state.get('lexware-prior'), st.session_state.get('lexware-once')),
        )
        st.markdown('''<style>
.st-key-lexware-action-card{border-radius:16px!important;border:1px solid #e3e8ef!important;box-shadow:0 6px 22px #16335b0a!important}
.st-key-lexware-action-card [data-testid="stVerticalBlockBorderWrapper"]{border:none!important;box-shadow:none!important}
.st-key-lexware-action-card [data-testid="stCheckbox"]{padding:.08rem 0;border-bottom:none!important}
.st-key-lexware-action-card [data-testid="stAlert"]{background:#eef4fd!important;border:1px solid #d3e2f7!important;color:#1c3a68!important;border-radius:10px!important}
.st-key-lexware-action-card [data-testid="stCaptionContainer"]{font-size:.78rem!important}
.st-key-lexware-status-badges{margin-top:12px;padding-top:14px;border-top:1px dashed #e3e8ef}
.st-key-lexware-status-badges [data-testid="stMetric"]{background:#f7f9fc!important;border:1px solid #e6eaf1!important;box-shadow:none!important;padding:10px 12px!important;min-height:auto!important}
.st-key-lexware-status-badges [data-testid="stMetricLabel"]{font-size:.74rem!important}
.st-key-lexware-status-badges [data-testid="stMetricValue"]{font-size:1.05rem!important}
</style>''', unsafe_allow_html=True)
        with st.container(border=True, key='lexware-action-card'):
            title_col,ref_col=st.columns([3,1],vertical_alignment='center')
            title_col.subheader('Lexware-Übertragung vorbereiten')
            if selected:
                ref_col.caption('Payout '+', '.join(selected))
            if totals:
                st.write(f"**Aktueller Umfang:** {len(chosen)} Positionen · {euros(totals['gross'])} brutto")
            check_col,status_col=st.columns([1,1],vertical_alignment='top')
            with check_col:
                st.checkbox('eBay-Geldeingang geprüft',key='lexware-received')
                st.checkbox('Kein bestehender Beleg in Lexware',key='lexware-prior')
                st.checkbox('Genau einen Entwurf erstellen',key='lexware-once')
            with status_col:
                if b_ready.empty:
                    st.warning('Aktuell sind keine neuen Positionen für Lexware freigegeben. Bereits übertragene Positionen bleiben gesperrt; weitere Positionen benötigen zuerst eine vollständig geklärte Payout-Zuordnung.')
                elif not api_key:
                    st.info(f'{len(chosen)} Positionen sind fachlich bereit. Für die Übermittlung den API-Key unter „Lexware-Verbindung“ hinterlegen und die drei Sicherheitsprüfungen bestätigen.')
                else:
                    st.success(f'{len(chosen)} Positionen sind fachlich bereit. Die Übermittlung wird erst nach allen drei Sicherheitsbestätigungen aktiv.')
                st.caption(f'{len(b_ready)} neu für Lexware bereit · {transmitted} bereits früher übertragen.')
            download_col,lexware_col=st.columns([1,1],vertical_alignment='center')
            with download_col:
                if totals:
                    download('Neue Evelyn-Abrechnung herunterladen',chosen,'Gruppe_B_Neu_Evelyn','group_b_evelyn')
            with lexware_col:
                create_clicked=st.button('An Lexware übermitteln',type='primary',disabled=not can_create,use_container_width=True,key='lexware-create',icon=':material/lock:')
            st.caption('Anzeige, Download und Lexware-Entwurf enthalten ausschließlich noch nicht übertragene, fachlich freigegebene Positionen. Frühere Entwürfe bleiben separat gebunden.')
            st.caption('Button-Sichtbarkeit ändert keine fachliche Freigabe oder Sperre.')

        with st.container(key='lexware-status-badges'):
            status_cols=st.columns(3)
            status_cols[0].metric('Neu abrechnungsfähig',f"{len(evelyn['new_ready'])} Positionen")
            status_cols[1].metric('Prüfung erforderlich',f"{len(evelyn['new_review'])} Positionen")
            status_cols[2].metric('Hold im neuen Payout',f"{len(evelyn['new_held'])} Positionen")
            st.caption('Neue Payouts seit dem letzten Beleg: '+(', '.join(evelyn['new_payouts']) if evelyn['new_payouts'] else 'keine'))
            st.caption(f"Hold-Positionen gesamt: {len(evelyn['held'])} · davon {len(evelyn['new_held'])} im neuen Payout · {len(evelyn['prior_held'])} aus älteren Payouts")
        if not evelyn['new_review'].empty:
            with st.expander(f"Prüfung erforderlich · {len(evelyn['new_review'])} neue Positionen"):
                st.dataframe(evelyn['new_review'][['Auszahlung Nr.','Bestellnummer','Partner','SKU','Erlös_Brutto','Bearbeitungsstatus']],hide_index=True,use_container_width=True)
        if not evelyn['new_held'].empty:
            with st.expander(f"Durch Hold blockiert · {len(evelyn['new_held'])} im neuen Payout"):
                st.dataframe(evelyn['new_held'][['Auszahlung Nr.','Bestellnummer','Partner','SKU','Erlös_Brutto','API_Hold_Hinweis']],hide_index=True,use_container_width=True)
        if not evelyn['prior_held'].empty:
            with st.expander(f"Ältere Hold-Positionen · {len(evelyn['prior_held'])} · nicht neu"):
                st.dataframe(evelyn['prior_held'][['Auszahlung Nr.','Bestellnummer','Partner','SKU','Erlös_Brutto','API_Hold_Hinweis']],hide_index=True,use_container_width=True)
        if not evelyn['refund_cases'].empty:
            with st.expander(f"Rückzahlungs-/Gutschriftfälle · {len(evelyn['refund_cases'])} bereits übertragene Positionen mit späterer Erstattung"):
                st.caption('Bereits an Lexware übertragene Positionen bleiben unverändert; hier nur zur separaten Klärung mit Evelyn.')
                st.dataframe(evelyn['refund_cases'][['Auszahlung Nr.','Bestellnummer','Partner','SKU','Erlös_Brutto','Erstattet_Brutto']],hide_index=True,use_container_width=True)

        with st.expander(f'Historie · {len(invoices)} Evelyn-Belege',expanded=False):
            st.markdown('**Bereits fakturiert**')
            if not active_invoices:
                st.caption('Noch kein Evelyn-Beleg gespeichert.')
            for item in active_invoices:
                invoice_date=studio_view.local_datetime(core.pd.Series([item['Datum']])).iloc[0].split()[0] if item['Datum'] else 'Datum nicht gespeichert'
                amount=euros(item['Betrag']) if item['Betrag'] is not None else 'Betrag nicht gespeichert'
                with st.container(border=True):
                    st.markdown(f"**{item['Belegnummer']}** · {invoice_date} · **{amount}** · {item['Positionen']} Positionen")
                    badge_col,payment_col,close_col=st.columns([.8,1.45,1],vertical_alignment='center')
                    badge_col.badge('fakturiert',color='blue',icon=':material/receipt_long:')
                    payment_col.badge('bezahlt' if item['Zahlungsstatus'].endswith('erhalten') else item['Zahlungsstatus'],color='green' if item['Zahlungsstatus'].endswith('erhalten') else 'orange',icon=':material/account_balance:')
                    close_col.badge('abgeschlossen' if item['Abschlussstatus']=='abgeschlossen' else 'Abschluss offen',color='green' if item['Abschlussstatus']=='abgeschlossen' else 'gray',icon=':material/check_circle:')
                    st.caption('Payouts: '+', '.join(item['Payouts']))
            st.divider()
            for item in sorted(invoices.values(),key=lambda record:record['Datum'],reverse=True):
                invoice_date=studio_view.local_datetime(core.pd.Series([item['Datum']])).iloc[0].split()[0] if item['Datum'] else 'Datum nicht gespeichert'
                amount=euros(item['Betrag']) if item['Betrag'] is not None else 'Betrag nicht gespeichert'
                st.markdown(f"**{item['Belegnummer']}** · {invoice_date} · {amount} · {item['Positionen']} Positionen")
                st.caption('Payouts: '+', '.join(item['Payouts']))
                st.caption(f"Zahlungsstatus: {item['Zahlungsstatus']} · Abschlussstatus: {item['Abschlussstatus']}")
                st.divider()
            if not transferred_rows.empty:
                invoice_map=dict(zip(states.Auszahlung,states.Entwurf))
                transferred_rows=transferred_rows.copy()
                transferred_rows['invoice_scope']=transferred_rows['Auszahlung Nr.'].map(invoice_map)
                for invoice_id,payment_rows in transferred_rows.groupby('invoice_scope'):
                    invoice_record=invoices.get(invoice_id,{})
                    st.caption(invoice_record.get('Belegnummer',str(invoice_id)))
                    outstanding=payment_rows[~payment_rows.received_at.astype(bool) & ~payment_rows.closed_at.astype(bool)]
                    if outstanding.empty:
                        st.checkbox('Zahlung von Evelyn erhalten',value=True,disabled=True,key='evelyn-paid-'+str(invoice_id))
                    else:
                        payment_key='evelyn-payment-'+str(invoice_id)
                        st.checkbox('Zahlung von Evelyn erhalten',key=payment_key,on_change=checkbox_confirmation,args=(payment_key,outstanding,'evelyn_received','Zahlung von Evelyn erhalten',invoice_id))
            else:
                st.checkbox('Zahlung von Evelyn erhalten',value=False,disabled=True,key='evelyn-payment-none')

        if create_clicked:
            try:
                expected={pid:core.payout_fingerprint(master[master['Auszahlung Nr.']==pid]) for pid in selected}
                for pid in selected:
                    core.confirm_received(pid)
                core.create_invoice_draft(api_key,selected,st.session_state.get('lexware-prior',False),expected_fingerprints=expected)
                _load_dashboard_data.clear()
                st.session_state['draft_created']=True
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


with pending:
    open_tab,refunds_tab,holds_tab,checks_tab=st.tabs(['Noch kein Payout','Gutschriften & Erstattungen','Einbehalte / Rücksendungen in Klärung','Zuordnungen & Gebühren'])
    with open_tab:
        st.subheader('Noch keinem Payout zugeordnet')
        st.caption('Offen, kein Fehler. Diese Positionen fließen noch in keine Abrechnung ein.')
        if open_orders.empty:
            st.info('Keine offenen Transaktionen ohne Payout.')
        else:
            st.dataframe(open_orders[['Bestellnummer','Datum','Partner','SKU','Produkttitel','Status']],hide_index=True,use_container_width=True)
            st.caption(f'{len(open_rows)} davon als noch nicht ausgezahlte Transaktionszeilen importiert. Weitere Bestellungen sind nur im Bestellbericht vorhanden.')
    with refunds_tab:
        refunds=business[(business.Art=='Erstattung') & ~business.closed_at.astype(bool)] if not business.empty else business
        if refunds.empty:
            st.info('Keine Erstattungen im Datenbestand.')
        else:
            for partner,block in refunds.groupby('Partner'):
                with st.container(border=True):
                    st.subheader(partner or 'Ohne Partnerzuordnung')
                    st.dataframe(block[['Bestellnummer','SKU','Angebotstitel','Erlös_Brutto','Auszahlung Nr.']].rename(columns={'Angebotstitel':'Produkttitel','Erlös_Brutto':'Payoutbetrag'}),hide_index=True,use_container_width=True)
                    download('Gutschriftenübersicht herunterladen',block[~block.reviewed_at.astype(bool)],'Gutschriften_'+partner)
                    workflow_panel(business[(business.Partner==partner) & (business.Art=='Erstattung')],'refund-'+partner,'refund')
                    if partner and block.Gruppe.isin(['Gruppe A','Gruppe B']).all():
                        invoice_panel(block,'refund-invoice-'+partner,'Gutschriften')
    with holds_tab:
        st.caption('API-Einbehalte sperren bestehende Bestellpositionen. Erstattungen bleiben getrennte Vorgänge. Bestehende manuelle Sperren und Rechnungszuordnungen bleiben erhalten.')
        if not business.empty:
            api_cases=business[api_holds.mask(business)]
            st.dataframe(api_cases[['Auszahlung Nr.','Bestellnummer','Partner','SKU','Erlös_Brutto','API_Hold_Hinweis','API_Korrekturfall','Lexware_uebertragen','Bearbeitungsstatus']],hide_index=True,use_container_width=True)
        st.dataframe(studio_view.holds(raw), hide_index=True, use_container_width=True)
    with checks_tab:
        if not master.empty:
            issues=master[master['Prüfhinweis']!='']
            if issues.empty:
                st.success('Alle bestellbezogenen Payoutpositionen sind zugeordnet.')
            else:
                st.dataframe(issues[['Auszahlung Nr.','Bestellnummer','SKU','Prüfhinweis']],hide_index=True,use_container_width=True)
            st.subheader('Partnerlose Gebühren')
            st.caption('Werden keinem Partner zugerechnet.')
            st.dataframe(master[master.Art=='Gebühr'][['Datum','Auszahlung Nr.','Angebotstitel','Erlös_Brutto']],hide_index=True,use_container_width=True)
            st.subheader('Bank-/Payout-Summenzeilen · nur Kontrollwerte')
            st.dataframe(raw[raw.Typ.str.strip().str.casefold()=='auszahlung'][['Auszahlung Nr.','Betrag abzügl. Kosten']],hide_index=True,use_container_width=True)

with history:
    ph,oh,lh,ih=st.tabs(['Payouts','Bestellberichte','Lexware','Eingangsrechnungen'])
    with ih:
        incoming=partner_invoices.list_invoices()
        if not incoming: st.info('Noch keine Partnerrechnungen hochgeladen.')
        for record in incoming:
            with st.container(border=True):
                invoice_report(record,'history-incoming-'+record['id'],allow_approval=False)
    with ph:
        if api_imports['runs']:
            with st.expander('eBay API · Abrufhistorie'):
                api_table=core.pd.DataFrame(api_imports['runs'])
                for field in ('at','start','end'):
                    api_table[field]=studio_view.local_datetime(api_table[field])
                st.dataframe(api_table[['source','trigger','at','start','end','new_payouts','new_transactions','known','ledger_only','status','error']].rename(columns={'source':'Quelle','trigger':'Auslösung','at':'Abruf','start':'Von','end':'Bis','new_payouts':'Neue Payouts','new_transactions':'Neue Finanzpositionen','known':'Bereits bekannt','ledger_only':'Nur API-Bewegungsnachweis','status':'Status','error':'Fehler'}),hide_index=True,use_container_width=True)
                controls=core.pd.DataFrame([{'Payout':pid,'API-Auszahlungsbetrag':euros(float(value['amount']['value'])),'API-Status':value['payoutStatus'],'Bewegungen':value['transactionCount']} for pid,value in api_imports['payouts'].items()])
                st.dataframe(controls,hide_index=True,use_container_width=True)
        table=core.pd.DataFrame(overview['history'])
        if not table.empty:
            table['Positionen']=table.Payoutnummer.map(master.groupby('Auszahlung Nr.').size())
            table['Status']=table.Payoutnummer.map(business_payout_status)
            table['Abschluss']=table.Status.map(lambda value:'Abgeschlossen' if value=='abgeschlossen' else 'Noch offen')
            st.dataframe(table.drop(columns=['Sperre']),hide_index=True,use_container_width=True)
            with st.expander('Historische Downloads'):
                pid=st.selectbox('Payout',sorted(master['Auszahlung Nr.'].unique()))
                state=states[states.Auszahlung==pid].iloc[0]
                block=master[master['Auszahlung Nr.']==pid]
                st.caption('Historische Downloads für den gewählten Payout; keine erneute Lexware-Erstellung.')
                for partner,rows in block[block.Gruppe.isin(['Gruppe A','Gruppe B'])].groupby('Partner'):
                    download(partner+' · Abrechnung herunterladen',rows,'Historie_'+pid+'_'+partner)
        else:
            st.info('Noch keine Payouts importiert.')
        if not overview['warnings'].empty:
            st.warning('Importpositionen zur manuellen Prüfung. Bestehende Payouts und Sperren wurden nicht verändert.')
            warning_table=overview['warnings'].copy()
            warning_table['at']=studio_view.local_datetime(warning_table['at'])
            st.dataframe(warning_table.rename(columns={'payout':'Payoutnummer','at':'Importdatum','reason':'Prüfhinweis'}),hide_index=True,use_container_width=True)
    with oh:
        logs=overview['imports']
        logs=logs[logs.kind=='orders'].copy()
        if logs.empty:
            st.info('Noch keine Bestellberichte importiert.')
        else:
            logs['at']=core.pd.to_datetime(logs['at'],utc=True).dt.tz_convert('Europe/Berlin').dt.strftime('%d.%m.%Y %H:%M').fillna('nicht bekannt')
            for col in ['start','end']:
                logs[col]=core.pd.to_datetime(logs[col]).dt.strftime('%d.%m.%Y').fillna('nicht bekannt')
            st.dataframe(logs[['filename','start','end','at','added','present','error']].rename(columns={'filename':'Bericht','start':'Von','end':'Bis','at':'Importdatum','added':'Neu','present':'Bereits vorhanden','error':'Prüfhinweis'}),hide_index=True,use_container_width=True)
    with lh:
        if not invoices:
            st.info('Noch keine Lexware-Entwürfe im Register.')
        for index,(invoice_id,item) in enumerate(invoices.items(),1):
            with st.container(border=True):
                st.subheader(f"Entwurf {index} · {item['Positionen'] if item['Positionen'] is not None else 'Anzahl unbekannt'} Positionen")
                st.write('Payouts: '+', '.join(item['Payouts']))
                st.caption(item['Status'])
                if not item['discarded'] and item['Positionen']:
                    if st.button('Lexware-Entwurf verwerfen', key='discard-'+invoice_id):
                        st.session_state['discard_request']=invoice_id
                with st.expander('Technische Details'):
                    st.code(invoice_id,language=None)

    if not business.empty:
        with st.expander('Positionshistorie · Prüfungen, Zahlungen und Abschlüsse'):
            st.dataframe(business[['Bestellnummer','Partner','SKU','Auszahlung Nr.','Bearbeitungsstatus','reviewed_at','paid_at','received_at','closed_at']].rename(columns={'reviewed_at':'Geprüft am','paid_at':'Partner bezahlt am','received_at':'Evelyn erhalten am','closed_at':'Abgeschlossen am'}),hide_index=True,use_container_width=True)

with dashboard:
    st.subheader('Projektübersicht')
    st.caption('Gesamter vorhandener Datenbestand · zugeordnete Payoutpositionen einschließlich Erstattungen. Einbehalte, offene Bestellungen, Gebühren und ungeklärte Zuordnungen sind ausgeschlossen.')
    try:
        counters=studio_view.project_totals(master)
        for col,label,value in zip(st.columns(3),['eBay-Umsatz gesamt','Provision Evelyn netto · 0,5 %','Provision Patrick netto · 3,0 %'],[counters['ebay'],counters['evelyn'],counters['patrick']]):
            col.metric(label,euros(value))
        st.caption('Provisionen auf der bestehenden Netto-Abrechnungsbasis. Patrick: nur Gruppe B, Differenz zwischen 3,5 % Partnerrabatt und 0,5 % Evelyn-Provision; Cent-Rundung wie in den Abrechnungen. Keine Aussage über bereits bezahlte Provisionen.')
    except ValueError as exc:
        st.warning('Kennzahlen benötigen eindeutige Quelldaten: '+str(exc))

with trust_risk_tab:
    trust_risk_ui.render(Path(core.PAYOUTS_DB_PATH).parent, catalogue, orders_master, raw)

if st.session_state.get('discard_request'):
    discard_dialog(st.session_state['discard_request'])
elif st.session_state.get('confirmation_request'):
    confirm_dialog(*st.session_state['confirmation_request'])
elif st.session_state.get('incoming_invoice_open'):
    incoming_invoice_dialog()
