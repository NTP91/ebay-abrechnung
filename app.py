import streamlit as st
from pathlib import Path
import core
import data_status
import studio_view
import position_workflow
import draft_correction
from datetime import date
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
        current=position_workflow.positions()
        if not current.empty:
            forbidden=set(current.loc[current.closed_at.astype(bool), 'position_key'])
            rows=rows[~rows.apply(position_workflow.position_key,axis=1).isin(forbidden)]
        if rows.empty:
            st.caption('Abgeschlossen · in der Historie archiviert; kein erneuter Export.')
            return
        blob = export_partner_excel(rows, statement_type=kind)
        st.download_button(label, blob, key+'.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', key=key)
    except ValueError as exc:
        st.warning(f'Export benötigt Prüfung: {exc}')


def partner_panel(rows, rate, prefix):
    if rows.empty:
        st.info('Aktuell keine offenen Partnerabrechnungen für diese Auswahl.')
        return
    try:
        summary = studio_view.partner_summary(rows)
    except ValueError as exc:
        st.warning(str(exc))
        return
    summary = summary.set_index('Partner')
    for partner, partner_block in rows.groupby('Partner'):
        with st.container(border=True):
            values=summary.loc[partner]
            st.subheader(partner)
            for col,label,value in zip(st.columns(4),['Offene Positionen','eBay-Brutto',f'Rabatt {rate} netto','Rechnungsbetrag brutto'],[str(len(partner_block)),euros(values['eBay-Brutto']),euros(values['Rabatt netto']),euros(values['Rechnungsbetrag'])]):
                col.metric(label,value)
            payout_ids=sorted(partner_block['Auszahlung Nr.'].unique())
            unreviewed=int((~partner_block.reviewed_at.astype(bool)).sum())
            status='Partnerrechnung geprüft' if not unreviewed else f'{unreviewed} '+('Position noch nicht geprüft' if unreviewed==1 else 'Positionen noch nicht geprüft')
            st.caption(f"Kumulative Sammelabrechnung aus {len(payout_ids)} Payout{'s' if len(payout_ids)!=1 else ''} · {status}. Payoutnummern bleiben in Export und Historie nachvollziehbar.")
            download_col,action_col,_=st.columns([1.3,1.5,2])
            with download_col:
                download('Einzelabrechnung herunterladen', partner_block, prefix+'_'+partner)
            with action_col:
                workflow_panel(partner_block,prefix+'_'+partner,action_only=True)


@st.dialog('Vorgang bestätigen')
def confirm_dialog(rows, action, label):
    st.write(f"**{label}** · {len(rows)} Positionen")
    st.write('Partner: '+', '.join(sorted(rows.Partner.unique())))
    st.write('Payouts: '+', '.join(sorted(rows['Auszahlung Nr.'].unique())))
    model=prepare_partner_export(rows,statement_type='group_b_evelyn' if action=='evelyn_received' else 'partner')
    st.write('Abrechnungsbetrag brutto: '+euros(sum(t['gross'] for t in model['totals'].values())))
    st.caption('Datum: '+date.today().strftime('%d.%m.%Y')+'. Nur den tatsächlich geprüften bzw. bezahlten Vorgang bestätigen.')
    st.dataframe(rows[['Bestellnummer','SKU','Angebotstitel','Erlös_Brutto']], hide_index=True)
    if st.button('Verbindlich bestätigen', type='primary'):
        try:
            position_workflow.confirm(rows.position_key.tolist(), action, date.today(),
                expected_sources={r.position_key:position_workflow.source_snapshot(r) for _,r in rows.iterrows()})
            st.session_state.pop('confirmation_request',None)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    if st.button('Abbrechen',key='cancel-confirmation'):
        st.session_state.pop('confirmation_request',None)
        st.rerun()


@st.dialog('Lexware-Entwurf verwerfen')
def discard_dialog(invoice_id):
    st.write('Den Entwurf zuerst in Lexware löschen. Danach prüft die App ausschließlich lesend, ob er nicht mehr vorhanden ist, und gibt seine Transfersperre frei. Partnerstatus und Historie bleiben erhalten.')
    st.link_button('Entwurf in Lexware öffnen', 'https://app.lexware.de/permalink/invoices/view/'+invoice_id)
    if st.button('Entwurf wurde in Lexware gelöscht – prüfen und freigeben', disabled=not api_key, type='primary'):
        try:
            draft_correction.discard(api_key, invoice_id, deleted_confirmed=True)
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
    active=rows[~rows['closed_at'].astype(bool) & ~rows['Prüfhinweis'].astype(bool) & ~rows.Quellenpruefung.astype(bool)]
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
                block=unreviewed
                actions=[('Partnerrechnung geprüft bestätigen' if mode=='partner' else 'Erstattung geprüft bestätigen','review')]
            elif mode=='refund':
                actions=[('Erstattung erledigt bestätigen','refund_settled')]
            else:
                block=block[~block.paid_at.astype(bool)]
                actions=[('Bezahlt / abgeschlossen' if block.empty or block.iloc[0].Gruppe=='Gruppe A' else 'Partner bezahlt','partner_paid')]
        if block.empty:
            continue
        for label, action in actions:
            if st.button(label, key=key+str(scope)+action):
                st.session_state['confirmation_request']=(block.copy(),action,label)


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
                            st.caption(f"{p['number']}: bereits vorhanden / keine neuen Daten"+(' · Lexware-Übertragung gesperrt' if p.get('locked') else ''))
                    if receipt['issues']:
                        st.warning(f"{receipt['issues']} Zuordnungen prüfen")
                else:
                    st.caption(f"{receipt['added']} neu · {receipt['present']} bereits vorhanden · {receipt['issues']} unvollständig")
                    if receipt.get('historical_without_sku'):
                        st.caption(f"{receipt['historical_without_sku']} historische Positionen ohne SKU archiviert · nicht abrechnungsrelevant")
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
    st.caption('Der automatische Belegabgleich folgt im nächsten Ausbauschritt. Bis dahin nur tatsächlich geprüfte Partnerrechnungen bestätigen.')

try:
    master=core.load_master_data()
    states=core.sync_status(master)
    overview=data_status.overview(master,states)
    ready=studio_view.eligible_rows(master,states)
    business=position_workflow.positions(master,states)
    partner_ready=studio_view.partner_rows(business)
    business_payout_status=position_workflow.payout_status(business)
    raw=core.read_master(core.PAYOUTS_DB_PATH)
    open_rows=studio_view.open_positions(raw)
    catalogue=studio_view.order_catalogue(raw,business)
    open_orders=catalogue[~catalogue.payout & (catalogue.Status!='Einbehalt / Rücksendung in Klärung')] if not catalogue.empty else catalogue
    invoices=studio_view.invoice_history()
except Exception as exc:
    st.error(f'Datenbestand benötigt Prüfung: {exc}')
    st.stop()
if st.session_state.pop('draft_created',False):
    st.success('Lexware-Entwurf erstellt. Die enthaltenen Positionen sind jetzt dauerhaft gesperrt.')

home, group_a, group_b, pending, history, dashboard = st.tabs(['Übersicht','Gruppe A','Gruppe B','Offene Positionen','Historie','Dashboard'])
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
        st.caption(f"{len(states)} Payouts im Bestand · {sum(s != 'abgeschlossen' for s in business_payout_status.values())} noch nicht abgeschlossen")
    with right,st.container(border=True):
        st.subheader('Nächster Schritt')
        st.write('**Zuordnungen prüfen**' if issue_count else '**Abrechnungen prüfen**' if not partner_ready.empty else '**Neue Berichte importieren**')
        st.caption('Partnerübersichten findest du in Gruppe A und Gruppe B. Erstattungen und Transaktionen ohne Payout stehen unter Offene Positionen.')
        st.write(f'{len(open_orders)} Bestellpositionen ohne bekannten Payout.')

with group_a:
    st.subheader('Gruppe A · Direktabrechnungen')
    st.caption('PP · BA · MK · 001 — 0,5 % Rabatt. Unabhängig von Patrick → Evelyn. Abschluss erst nach Prüfung und bestätigter Partnerzahlung.')
    with st.container(border=True):
        partner_panel(partner_ready[partner_ready.Gruppe=='Gruppe A'] if not partner_ready.empty else partner_ready,'0,5 %','Gruppe_A')

with group_b:
    b_ready=ready[ready.Gruppe=='Gruppe B'] if not ready.empty else ready
    b_open=business[(business.Gruppe=='Gruppe B') & (business.Art=='Bestellung') & (business['Erlös_Brutto']>0) & ~business.closed_at.astype(bool) & ~business['Prüfhinweis'].astype(bool) & ~business.Quellenpruefung.astype(bool)] if not business.empty else business
    with st.container(border=True):
        st.subheader('Partner → Patrick')
        st.caption('Einzelabrechnungen für MH, NB und weitere zugeordnete Partner · 3,5 % Rabatt')
        partner_panel(partner_ready[partner_ready.Gruppe=='Gruppe B'] if not partner_ready.empty else partner_ready,'3,5 %','Partner_Patrick')
    with st.container(border=True):
        st.subheader('Gesamtabrechnung Gruppe B an Evelyn')
        transmitted=sum(item['Positionen'] or 0 for item in invoices.values() if not item['discarded'])
        available=sorted(b_ready['Auszahlung Nr.'].unique()) if not b_ready.empty else []
        selected=available
        chosen=b_ready
        totals=None
        if not chosen.empty:
            try:
                totals=prepare_partner_export(chosen,statement_type='group_b_evelyn')['totals']['Rechnung']
            except ValueError as exc:
                st.warning(str(exc))

        with st.container(border=True):
            action_col,status_col=st.columns([1.15,1.85],vertical_alignment='top')
            with action_col:
                st.markdown('**Lexware-Aktion**')
                received=st.checkbox('eBay-Geldeingang geprüft',key='lexware-received')
                prior_checked=st.checkbox('Kein bestehender Beleg in Lexware',key='lexware-prior')
                confirmed=st.checkbox('Genau einen Entwurf erstellen',key='lexware-once')
                can_create=bool(selected and totals and api_key and received and prior_checked and confirmed)
                create_clicked=st.button('An Lexware übermitteln',type='primary',disabled=not can_create,use_container_width=True,key='lexware-create')
            with status_col:
                if b_ready.empty:
                    st.warning('Aktuell sind keine neuen Positionen für Lexware freigegeben. Bereits übertragene Positionen bleiben gesperrt; weitere Positionen benötigen zuerst eine vollständig geklärte Payout-Zuordnung.')
                elif not api_key:
                    st.info(f'{len(chosen)} Positionen sind fachlich bereit. Für die Übermittlung den API-Key unter „Lexware-Verbindung“ hinterlegen und die drei Sicherheitsprüfungen bestätigen.')
                else:
                    st.success(f'{len(chosen)} Positionen sind fachlich bereit. Die Übermittlung wird erst nach allen drei Sicherheitsbestätigungen aktiv.')
                st.caption(f'{len(b_ready)} neu für Lexware bereit · {transmitted} bereits früher übertragen. Button-Sichtbarkeit ändert keine fachliche Freigabe oder Sperre.')
                if totals:
                    st.write(f"**Neuer Entwurfsumfang:** {len(chosen)} Positionen · {euros(totals['gross'])} brutto")
                    st.caption('Payoutnachweise: '+', '.join(selected))
            if create_clicked:
                try:
                    expected={pid:core.payout_fingerprint(master[master['Auszahlung Nr.']==pid]) for pid in selected}
                    for pid in selected:
                        core.confirm_received(pid)
                    core.create_invoice_draft(api_key,selected,prior_checked,expected_fingerprints=expected)
                    st.session_state['draft_created']=True
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

        if not b_open.empty:
            try:
                all_totals=prepare_partner_export(b_open,statement_type='group_b_evelyn')['totals']['Rechnung']
                for col,label,value in zip(st.columns(4),['Offene Positionen','Netto vor Rabatt','Rabatt 0,5 % netto','Rechnungsbetrag brutto'],[str(len(b_open)),euros(all_totals['net']),euros(all_totals['discount']),euros(all_totals['gross'])]):
                    col.metric(label,value)
                download('Gesamtübersicht herunterladen',b_open,'Gruppe_B_Gesamt_Evelyn','group_b_evelyn')
            except ValueError as exc:
                st.warning('Gesamtübersicht benötigt Prüfung: '+str(exc))
        st.caption('Die Gesamtübersicht enthält alle aktuell offenen Gruppe-B-Positionen. Der Lexware-Entwurf enthält ausschließlich noch nicht übertragene, fachlich freigegebene Positionen.')
        if not business.empty:
            workflow_panel(business[business.Lexware_uebertragen],'b-evelyn','evelyn')


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
                    download('Gutschriftenübersicht herunterladen',block,'Gutschriften_'+partner)
                    workflow_panel(business[(business.Partner==partner) & (business.Art=='Erstattung')],'refund-'+partner,'refund')
    with holds_tab:
        st.caption('Einbehalte sind keine Abrechnungspositionen und kein Fehler. Die ursprünglichen Referenzen bleiben erhalten. Spätere Auszahlungen und Erstattungen werden als eigene Folgebewegungen verarbeitet.')
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

with history:
    ph,oh,lh=st.tabs(['Payouts','Bestellberichte','Lexware'])
    with ph:
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

if st.session_state.get('discard_request'):
    discard_dialog(st.session_state['discard_request'])
elif st.session_state.get('confirmation_request'):
    confirm_dialog(*st.session_state['confirmation_request'])
