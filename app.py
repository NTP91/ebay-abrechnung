import streamlit as st
from pathlib import Path
import core
import data_status
import studio_view
from partner_export import export_partner_excel, prepare_partner_export

st.set_page_config(page_title='Payout Studio', page_icon='💠', layout='wide')
st.markdown('''<style>
.stApp{background:#f5f7fb;color:#17243c}
[data-testid="stHeader"]{background:transparent}
[data-testid="stToolbar"]{visibility:hidden}
[data-testid="stSidebar"]{background:#fff;border-right:1px solid #e6eaf1}
.block-container{max-width:1500px;padding-top:2rem;padding-bottom:3rem}
h1,h2,h3{color:#17243c;letter-spacing:-.025em}h1{font-size:2rem!important}h3{font-size:1.15rem!important}
[data-testid="stCaptionContainer"]{color:#69768d}
[data-testid="stMetric"]{background:#fff;border:1px solid #e4e9f2;border-radius:14px;padding:18px;box-shadow:0 2px 5px #17243c04}
[data-testid="stMetricLabel"]{color:#66738b;font-size:.82rem}
[data-testid="stMetricValue"]{color:#15243d;font-size:1.8rem}
[data-testid="stVerticalBlockBorderWrapper"]{background:#fff;border-radius:14px}
[data-testid="stDataFrame"]{border:1px solid #edf0f5;border-radius:9px}
button{border-radius:8px!important;font-weight:550!important}
[data-testid="stBaseButton-primary"]{background:#246bfe;border-color:#246bfe;color:white}
[data-testid="stBaseButton-primary"]:disabled{background:#e9eef8;border-color:#e9eef8;color:#8995ab}
[data-baseweb="tab-list"]{gap:24px;border-bottom:1px solid #e5eaf2;margin-bottom:20px}
[data-baseweb="tab"]{padding:12px 4px;color:#64718a}
[data-baseweb="tab"][aria-selected="true"]{color:#2165eb}
[data-baseweb="tab-highlight"]{background:#246bfe}
[data-testid="stFileUploaderDropzone"]{background:#f6f8fc;border:1px dashed #dce3ef;border-radius:10px}
[data-testid="stExpander"]{border-color:#e5eaf2;border-radius:10px}
</style>''', unsafe_allow_html=True)


def euros(value):
    return f'{value:,.2f} €'.replace(',', 'X').replace('.', ',').replace('X', '.')


def download(label, rows, key, kind='partner'):
    if rows.empty:
        return
    try:
        blob = export_partner_excel(rows, statement_type=kind)
        st.download_button(label, blob, key+'.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', key=key)
    except ValueError as exc:
        st.warning(f'Export benötigt Prüfung: {exc}')


def partner_panel(rows, rate, prefix):
    if rows.empty:
        st.info('Aktuell keine neuen abrechenbaren Positionen.')
        return
    try:
        summary = studio_view.partner_summary(rows)
    except ValueError as exc:
        st.warning(str(exc))
        return
    summary = summary.rename(columns={'Rabatt netto':f'Rabatt {rate} netto','Rechnungsbetrag':'Rechnungsbetrag brutto'})
    st.dataframe(summary, hide_index=True, use_container_width=True,
                 column_config={c:st.column_config.NumberColumn(c,format='%.2f €') for c in summary if c not in ('Partner','Positionen')})
    st.markdown(f"**{int(summary.Positionen.sum())} Positionen · {euros(summary['Rechnungsbetrag brutto'].sum())} gesamt**")
    for partner, block in rows.groupby('Partner'):
        label, action = st.columns([2,3], vertical_alignment='center')
        label.write(f'**{partner}** · {len(block)} Positionen')
        with action:
            download('Einzelabrechnung herunterladen', block, prefix+'_'+partner)


with st.sidebar:
    st.markdown('### 💠 Payout Studio')
    st.caption('RECOVERY · PATRICKS ARBEITSBEREICH')
    st.divider()
    st.subheader('Import & Dateien')
    payouts = st.file_uploader('Transaktionsberichte', type=['csv'], accept_multiple_files=True)
    orders = st.file_uploader('Bestellberichte', type=['csv','xlsx'], accept_multiple_files=True)
    if st.button('Dateien importieren', type='primary', disabled=not(payouts or orders), use_container_width=True):
        try:
            receipts=[]
            for kind, files in [('orders',orders),('payout',payouts)]:
                for uploaded in files:
                    receipts.append(data_status.import_file(uploaded,kind))
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
                            st.caption(f"{p['number']}: bereits vorhanden / keine neuen Daten"+(' · gesperrt' if p.get('locked') else ''))
                    if receipt['issues']:
                        st.warning(f"{receipt['issues']} Zuordnungen prüfen")
                else:
                    st.caption(f"{receipt['added']} neu · {receipt['present']} bereits vorhanden · {receipt['issues']} unvollständig")
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
try:
    master=core.load_master_data()
    states=core.sync_status(master)
    overview=data_status.overview(master,states)
    ready=studio_view.eligible_rows(master,states)
    raw=core.read_master(core.PAYOUTS_DB_PATH)
    open_rows=studio_view.open_positions(raw)
    invoices=studio_view.invoice_history()
except Exception as exc:
    st.error(f'Datenbestand benötigt Prüfung: {exc}')
    st.stop()
if st.session_state.pop('draft_created',False):
    st.success('Lexware-Entwurf erstellt. Die enthaltenen Positionen sind jetzt dauerhaft gesperrt.')

home, group_a, group_b, pending, history = st.tabs(['Übersicht','Gruppe A','Gruppe B','Offene Positionen','Historie'])
with home:
    total,assigned,without=studio_view.order_metrics(raw)
    issue_count=int(master['Prüfhinweis'].astype(bool).sum()) if not master.empty else 0
    for col,label,value in zip(st.columns(5),['Bestellungen gesamt','Einem Payout zugeordnet','Noch ohne Payout','Neu abrechenbare Positionen','Zuordnungen prüfen'],[total,assigned,without,len(ready),issue_count]):
        col.metric(label,value)
    st.caption('Bestelltransaktionen der importierten Transaktionsberichte; Erstattungen und Gebühren sind separat.')
    if issue_count:
        st.warning(f'{issue_count} Zuordnungen prüfen. Betroffene Payouts bleiben gesperrt.')
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
        st.caption(f"{len(states)} Payouts im Bestand · {overview['unbilled']} noch nicht abgerechnet")
    with right,st.container(border=True):
        st.subheader('Nächster Schritt')
        st.write('**Zuordnungen prüfen**' if issue_count else '**Abrechnungen prüfen**' if not ready.empty else '**Neue Berichte importieren**')
        st.caption('Partnerübersichten findest du in Gruppe A und Gruppe B. Erstattungen und Transaktionen ohne Payout stehen unter Offene Positionen.')
        st.write(f'{len(open_rows)} Transaktionen warten auf einen Payout.')

with group_a:
    st.subheader('Gruppe A · Direktabrechnungen')
    st.caption('PP · BA · MK · 001 — 0,5 % Rabatt. Neue positive Positionen ungesperrter Payouts.')
    with st.container(border=True):
        partner_panel(ready[ready.Gruppe=='Gruppe A'] if not ready.empty else ready,'0,5 %','Gruppe_A')

with group_b:
    b_ready=ready[ready.Gruppe=='Gruppe B'] if not ready.empty else ready
    with st.container(border=True):
        st.subheader('Partner → Patrick')
        st.caption('Einzelabrechnungen für MH, NB und weitere zugeordnete Partner · 3,5 % Rabatt')
        partner_panel(b_ready,'3,5 %','Partner_Patrick')
    with st.container(border=True):
        st.subheader('Patrick → Evelyn')
        transmitted=sum(item['Positionen'] or 0 for item in invoices.values())
        st.caption(f'{len(b_ready)} neue Positionen für Lexware bereit · {transmitted} Positionen bereits früher übertragen')
        if b_ready.empty:
            st.info('Keine neuen Gruppe-B-Positionen für einen Lexware-Entwurf.')
            st.button('Lexware-Entwurf erstellen', type='primary', disabled=True, use_container_width=True)
        else:
            available=sorted(b_ready['Auszahlung Nr.'].unique())
            selected=st.multiselect('Payouts für die Gesamtrechnung',available,default=available)
            chosen=b_ready[b_ready['Auszahlung Nr.'].isin(selected)]
            totals=None
            if not chosen.empty:
                try:
                    totals=prepare_partner_export(chosen,statement_type='group_b_evelyn')['totals']['Rechnung']
                    for col,label,value in zip(st.columns(4),['Positionen','Netto vor Rabatt','Rabatt 0,5 % netto','Rechnungsbetrag brutto'],[str(len(chosen)),euros(totals['net']),euros(totals['discount']),euros(totals['gross'])]):
                        col.metric(label,value)
                    st.caption(f"Netto nach Rabatt: {euros(totals['net_after'])} · 19 % Umsatzsteuer: {euros(totals['tax'])}")
                    st.caption('eBay-Auszahlungsnummern: '+', '.join(selected))
                    download('Gesamtabrechnung herunterladen',chosen,'Gruppe_B_Gesamt_Evelyn','group_b_evelyn')
                except ValueError as exc:
                    st.warning(str(exc))
            received=st.checkbox('Geldeingang für alle ausgewählten Payouts geprüft')
            prior_checked=st.checkbox('In Lexware geprüft: Für diese Payouts besteht noch keine Rechnung.')
            confirmed=st.checkbox('Genau einen neuen Entwurf erstellen, nicht finalisieren oder versenden.')
            if not api_key:
                st.caption('API-Key bei Bedarf unter „Lexware-Verbindung“ hinterlegen.')
            if st.button('Lexware-Entwurf erstellen',type='primary',disabled=not(selected and totals and api_key and received and prior_checked and confirmed),use_container_width=True):
                try:
                    expected={pid:core.payout_fingerprint(master[master['Auszahlung Nr.']==pid]) for pid in selected}
                    for pid in selected:
                        core.confirm_received(pid)
                    core.create_invoice_draft(api_key,selected,prior_checked,expected_fingerprints=expected)
                    st.session_state['draft_created']=True
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

with pending:
    open_tab,refunds_tab,checks_tab=st.tabs(['Noch kein Payout','Gutschriften & Erstattungen','Zuordnungen & Gebühren'])
    with open_tab:
        st.subheader('Noch keinem Payout zugeordnet')
        st.caption('Offen, kein Fehler. Diese Positionen fließen noch in keine Abrechnung ein.')
        if open_rows.empty:
            st.info('Keine offenen Transaktionen ohne Payout.')
        else:
            st.dataframe(open_rows,hide_index=True,use_container_width=True)
    with refunds_tab:
        refunds=master[master.Art=='Erstattung'] if not master.empty else master
        if refunds.empty:
            st.info('Keine Erstattungen im Datenbestand.')
        else:
            for partner,block in refunds.groupby('Partner'):
                with st.container(border=True):
                    st.subheader(partner or 'Ohne Partnerzuordnung')
                    st.dataframe(block[['Bestellnummer','SKU','Angebotstitel','Erlös_Brutto','Auszahlung Nr.']].rename(columns={'Angebotstitel':'Produkttitel','Erlös_Brutto':'Payoutbetrag'}),hide_index=True,use_container_width=True)
                    download('Gutschriftenübersicht herunterladen',block,'Gutschriften_'+partner)
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

with history:
    ph,oh,lh=st.tabs(['Payouts','Bestellberichte','Lexware'])
    with ph:
        table=core.pd.DataFrame(overview['history'])
        if not table.empty:
            table['Positionen']=table.Payoutnummer.map(master.groupby('Auszahlung Nr.').size())
            table['Abrechnung']=table.Payoutnummer.map({r.Auszahlung:'Bereits abgerechnet' if r.Entwurf else 'Gesperrt / prüfen' if r.Sperre else 'Noch offen' for r in states.itertuples()})
            st.dataframe(table.drop(columns=['Sperre']),hide_index=True,use_container_width=True)
            with st.expander('Historische Downloads & Statuspflege'):
                pid=st.selectbox('Payout',sorted(master['Auszahlung Nr.'].unique()))
                state=states[states.Auszahlung==pid].iloc[0]
                block=master[master['Auszahlung Nr.']==pid]
                st.caption('Historische Downloads für den gewählten Payout; keine erneute Lexware-Erstellung.')
                for partner,rows in block[block.Gruppe.isin(['Gruppe A','Gruppe B'])].groupby('Partner'):
                    download(partner+' · Abrechnung herunterladen',rows,'Historie_'+pid+'_'+partner)
                target=core.FOLLOWUP.get(state.Status)
                if target:
                    checked=st.checkbox('Manuell geprüft: '+target)
                    if st.button('Status bestätigen',disabled=not checked):
                        try:
                            core.advance_status(pid,target)
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))
        else:
            st.info('Noch keine Payouts importiert.')
        if not overview['warnings'].empty:
            st.warning('Importpositionen zur manuellen Prüfung. Bestehende Payouts und Sperren wurden nicht verändert.')
            st.dataframe(overview['warnings'].rename(columns={'payout':'Payoutnummer','at':'Importdatum','reason':'Prüfhinweis'}),hide_index=True,use_container_width=True)
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
                st.caption(item['Status']+' · dauerhaft von neuen Entwürfen ausgeschlossen')
                with st.expander('Technische Details'):
                    st.code(invoice_id,language=None)
