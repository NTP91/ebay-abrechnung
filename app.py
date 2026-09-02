import streamlit as st
import pandas as pd
import io
import re
import requests
import json
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

st.set_page_config(page_title="eBay Payout & Lexoffice Automatisierung", layout="wide")

st.title("⚡ eBay Payout & Lexoffice Direct-Upload")

# Sidebar für Zugangsdaten
st.sidebar.header("🔑 Lexoffice API Konfiguration")
lexoffice_api_key = "Wciy230Sw_pNI7.yFDyNsWuvvXIB2sxJ2MKLk2jfMowyWJKU", type="password")
customer_name_search = st.sidebar.text_input("2. Exakter Kundenname in Lexoffice:", value="Evelyn")

# Uploads
col1, col2 = st.columns(2)
with col1:
    uploaded_payout = st.file_uploader("1. eBay Auszahlungsberichte (CSV)", type=["csv"], accept_multiple_files=True, key="payout")
with col2:
    uploaded_invoice = st.file_uploader("2. Soll-Rechnung / Referenz (Excel/CSV)", type=["xlsx", "csv"], key="invoice")

# Positionen-Modus
position_mode = st.radio(
    "Wie sollen die Positionen in Lexoffice angelegt werden?",
    ["Option A: Sammelpositionen pro Partner + PDF-Einzelnachweis (Empfohlen)", "Option B: Jede Bestellung als einzelne Position in Lexoffice"]
)

# Hilfsfunktionen
def parse_german_float(val):
    if pd.isna(val) or val == '--' or str(val).strip() == '':
        return 0.0
    return float(str(val).replace('.', '').replace(',', '.'))

def clean_order_number(val):
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if s.endswith('.0'):
        s = s[:-2]
    s = re.sub(r'[^A-Za-z0-9]', '', s).upper()
    s = s.lstrip('0')
    return s

def extract_partner_prefix(sku):
    if pd.isna(sku) or str(sku).strip() in ['--', '']:
        return 'OHNE_SKU'
    sku_clean = str(sku).strip().upper()
    raw_prefix = sku_clean.split('/')[0].strip()
    if raw_prefix.startswith('001') or raw_prefix == '001':
        return '001'
    match = re.match(r'^([A-Z]+)', raw_prefix)
    if match:
        return match.group(1)
    return raw_prefix

def generate_pdf_report(title_text, summary_df, details_df):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), rightMargin=20, leftMargin=20, topMargin=20, bottomMargin=20)
    story = []
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontSize=16, leading=20, textColor=colors.HexColor("#1A365D"))
    subtitle_style = ParagraphStyle('SubTitleStyle', parent=styles['Heading2'], fontSize=12, leading=16, textColor=colors.HexColor("#2B6CB0"))
    cell_style = ParagraphStyle('CellStyle', parent=styles['Normal'], fontSize=8, leading=10)
    header_cell_style = ParagraphStyle('HeaderCellStyle', parent=styles['Normal'], fontSize=8, leading=10, textColor=colors.white, fontName="Helvetica-Bold")
    
    story.append(Paragraph(title_text, title_style))
    story.append(Spacer(1, 10))
    story.append(Paragraph("1. Zusammenfassung nach Partnern", subtitle_style))
    story.append(Spacer(1, 5))
    
    sum_data = [[Paragraph(col, header_cell_style) for col in summary_df.columns]]
    for _, row in summary_df.iterrows():
        sum_data.append([Paragraph(str(val), cell_style) for val in row])
        
    t_sum = Table(sum_data)
    t_sum.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#2B6CB0")),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('TOPPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(t_sum)
    story.append(Spacer(1, 15))
    
    story.append(Paragraph("2. Einzelnachweis aller Transaktionen", subtitle_style))
    story.append(Spacer(1, 5))
    
    det_cols = ['Datum', 'Bestellnummer', 'SKU', 'Stück', 'Erlös Brutto (€)', 'Abrechnungsbetrag (€)']
    det_df = details_df[['Datum der Transaktionserstellung', 'Bestellnummer', 'SKU', 'Menge', 'eBay_Brutto', 'Auszahlung_Evelyn_Brutto']].copy()
    det_df.columns = det_cols
    
    det_data = [[Paragraph(col, header_cell_style) for col in det_cols]]
    for _, row in det_df.iterrows():
        r_list = []
        for col in det_cols:
            val = row[col]
            if isinstance(val, float):
                val = f"{val:.2f} €".replace('.', ',')
            r_list.append(Paragraph(str(val), cell_style))
        det_data.append(r_list)
        
    t_det = Table(det_data)
    t_det.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#4A5568")),
        ('GRID', (0,0), (-1,-1), 0.5, colors.lightgrey),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('TOPPADDING', (0,0), (-1,-1), 3),
    ]))
    story.append(t_det)
    
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

# Lexoffice API Funktionen
def get_lexoffice_contact_id(api_key, name):
    headers = {"Authorization": f"Bearer {api_key}"}
    res = requests.get(f"https://api.lexoffice.io/v1/contacts?name={name}", headers=headers)
    if res.status_code == 200:
        data = res.json()
        if data.get('content'):
            return data['content'][0]['id']
    return None

def create_lexoffice_invoice(api_key, contact_id, line_items, remark):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {
        "archived": False,
        "voucherDate": pd.Timestamp.now().strftime("%Y-%m-%d"),
        "address": {"contactId": contact_id},
        "lineItems": line_items,
        "totalPrice": {"currency": "EUR"},
        "taxConditions": {"taxType": "net"},
        "remark": remark
    }
    res = requests.post("https://api.lexoffice.io/v1/invoices?finalize=false", headers=headers, json=payload)
    if res.status_code in [200, 201]:
        return res.json()['id']
    else:
        st.error(f"Fehler beim Erstellen der Rechnung in Lexoffice: {res.text}")
        return None

def upload_lexoffice_document(api_key, invoice_id, pdf_bytes, filename):
    headers = {"Authorization": f"Bearer {api_key}"}
    files = {"file": (filename, pdf_bytes, "application/pdf")}
    res = requests.post(f"https://api.lexoffice.io/v1/invoices/{invoice_id}/document", headers=headers, files=files)
    return res.status_code in [200, 201]

GROUP_A_PREFIXES = ['PP', 'BA', 'MK', '001']

if uploaded_payout:
    try:
        all_dfs = []
        processed_file_names = set()
        
        for file in uploaded_payout:
            if file.name in processed_file_names:
                continue
            processed_file_names.add(file.name)
            content = file.getvalue().decode('utf-8', errors='ignore')
            lines = content.splitlines()
            header_idx = 0
            for i, line in enumerate(lines):
                if "Bestellnummer" in line or "Datum der Transaktionserstellung" in line:
                    header_idx = i
                    break
            df_temp = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), sep=';')
            all_dfs.append(df_temp)
        
        df_payout = pd.concat(all_dfs, ignore_index=True)
        
        df_payout['Bestellnummer_Match'] = df_payout['Bestellnummer'].apply(clean_order_number)
        df_payout = df_payout.drop_duplicates(subset=['Bestellnummer_Match', 'Datum der Transaktionserstellung', 'Betrag abzügl. Kosten'])
        
        df_payout['eBay_Brutto'] = df_payout['Betrag abzügl. Kosten'].apply(parse_german_float)
        df_payout['eBay_Netto'] = (df_payout['eBay_Brutto'] / 1.19).round(2)
        
        df_payout['SKU'] = df_payout['Bestandseinheit'].fillna('OHNE_SKU').astype(str).str.strip()
        df_payout['SKU_Prefix'] = df_payout['SKU'].apply(extract_partner_prefix)
        df_payout['Menge'] = 1

        df_payout['Gruppe'] = df_payout['SKU_Prefix'].apply(
            lambda p: 'Gruppe A (Direkt)' if p in GROUP_A_PREFIXES else ('Ohne Zuordnung' if p == 'OHNE_SKU' else 'Gruppe B (Über Dich)')
        )
        
        df_payout['Evelyn_Prov_EUR'] = (df_payout['eBay_Brutto'] * 0.005).round(2)
        df_payout['Auszahlung_Evelyn_Brutto'] = (df_payout['eBay_Brutto'] - df_payout['Evelyn_Prov_EUR']).round(2)

        df_grp_b = df_payout[df_payout['Gruppe'] == 'Gruppe B (Über Dich)'].copy()

        if not df_grp_b.empty:
            st.markdown("---")
            st.subheader("📊 Vorschau für Lexoffice")
            
            summary_b = df_grp_b.groupby('SKU_Prefix').agg(
                Anzahl_Transaktionen=('SKU', 'count'),
                Erlös_Netto=('eBay_Netto', 'sum'),
                Erlös_Brutto=('eBay_Brutto', 'sum'),
                Evelyn_Provision=('Evelyn_Prov_EUR', 'sum'),
                Auszahlungsbetrag=('Auszahlung_Evelyn_Brutto', 'sum')
            ).reset_index()

            summary_b['Rabatt_Prozent'] = "0,5 %"
            st.dataframe(summary_b, use_container_width=True)

            pdf_bytes = generate_pdf_report("Rechnungsanlage – Gesamtabrechnung Gruppe B", summary_b, df_grp_b)

            st.markdown("---")
            if st.button("🚀 JETZT AUTOMATISCH IN LEXOFFICE ANLEGEN", type="primary"):
                if not lexoffice_api_key:
                    st.error("Bitte trage zuerst deinen Lexoffice API-Key in der linken Seitenleiste ein!")
                else:
                    with st.spinner("Suche Kunde in Lexoffice..."):
                        contact_id = get_lexoffice_contact_id(lexoffice_api_key, customer_name_search)
                    
                    if not contact_id:
                        st.error(f"Kunde '{customer_name_search}' wurde in Lexoffice nicht gefunden! Bitte stelle sicher, dass der Kunde unter Kontakte in Lexoffice existiert.")
                    else:
                        line_items = []
                        if "Option A" in position_mode:
                            for _, r in summary_b.iterrows():
                                line_items.append({
                                    "type": "custom",
                                    "name": f"Abrechnung Partner {r['SKU_Prefix']} ({r['Anzahl_Transaktionen']} Stück laut Anlage)",
                                    "quantity": 1,
                                    "unitName": "Stück",
                                    "unitPrice": {"currency": "EUR", "netAmount": round(r['Erlös_Netto'], 2), "taxRatePercentage": 19},
                                    "discountPercentage": 0.5
                                })
                        else:
                            for _, r in df_grp_b.iterrows():
                                line_items.append({
                                    "type": "custom",
                                    "name": f"Bestellung {r['Bestellnummer']} | SKU: {r['SKU']}",
                                    "quantity": 1,
                                    "unitName": "Stück",
                                    "unitPrice": {"currency": "EUR", "netAmount": round(r['eBay_Netto'], 2), "taxRatePercentage": 19},
                                    "discountPercentage": 0.5
                                })
                        
                        remark_text = "Die detaillierte Einzelaufstellung der Verkäufe und Rückerstattungen entnehmen Sie bitte der beigefügten Anlage."
                        
                        with st.spinner("Erstelle Rechnungsentwurf in Lexoffice..."):
                            inv_id = create_lexoffice_invoice(lexoffice_api_key, contact_id, line_items, remark_text)
                        
                        if inv_id:
                            st.success(f"✅ Rechnung erfolgreich als Entwurf in Lexoffice angelegt! (ID: {inv_id})")
                            
                            with st.spinner("Hänge PDF-Einzelnachweis an den Entwurf an..."):
                                ok = upload_lexoffice_document(lexoffice_api_key, inv_id, pdf_bytes, "Rechnungsanlage_Details.pdf")
                            
                            if ok:
                                st.balloons()
                                st.success("🎉 PERFEKT: Die PDF-Anlage wurde unrennbar an die Lexoffice-Rechnung angehängt!")
                            else:
                                st.warning("Rechnung wurde angelegt, aber das PDF konnte nicht angehängt werden.")

    except Exception as e:
        st.error(f"Fehler: {e}")
