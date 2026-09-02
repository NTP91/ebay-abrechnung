import os
import glob
import pandas as pd
import io

ORDERS_DB_PATH = "Master_Orders.csv"
PAYOUTS_DB_PATH = "Master_Payouts.csv"

def load_master_data():
    """Lädt Payouts und Orders und verknüpft sie über die Bestellnummer."""
    if not os.path.exists(PAYOUTS_DB_PATH):
        return pd.DataFrame()

    try:
        payouts = pd.read_csv(PAYOUTS_DB_PATH, sep=';', dtype=str)
    except Exception:
        return pd.DataFrame()

    if payouts.empty:
        return pd.DataFrame()

    orders = pd.DataFrame()
    if os.path.exists(ORDERS_DB_PATH):
        try:
            orders = pd.read_csv(ORDERS_DB_PATH, sep=';', dtype=str)
        except Exception:
            orders = pd.DataFrame()

    # Mapping aus Orders
    order_map = {}
    if not orders.empty:
        cols_norm = {str(c).lower().replace('-', '').replace('_', '').replace(' ', ''): c for c in orders.columns}
        
        oid_col = next((cols_norm[k] for k in ['bestellnummer', 'orderid', 'ordernumber', 'verkaufsnummer', 'verkaufsprotokollnr', 'transaktionsid'] if k in cols_norm), None)
        sku_col = next((cols_norm[k] for k in ['sku', 'customlabel', 'eigenesku', 'bestandseinheit', 'angebotsnummer', 'artikelnummer'] if k in cols_norm), None)
        title_col = next((cols_norm[k] for k in ['artikelname', 'artikeltitel', 'itemtitle', 'artikelbezeichnung', 'title', 'angebotstitel'] if k in cols_norm), None)

        if oid_col:
            for _, row in orders.iterrows():
                oid = str(row[oid_col]).strip()
                if oid and oid not in ['nan', 'None', '']:
                    sku_v = str(row[sku_col]).strip() if sku_col and pd.notna(row[sku_col]) else 'NB /'
                    title_v = str(row[title_col]).strip() if title_col and pd.notna(row[title_col]) else '--'
                    order_map[oid] = {'SKU': sku_v, 'Angebotstitel': title_v}

    processed = []
    prefixes_a = ["PP", "BA", "MK", "001"]

    for _, row in payouts.iterrows():
        bestellnr = '--'
        for col in ['Bestellnummer', 'Order ID', 'Verkaufsnummer']:
            if col in row and pd.notna(row[col]) and str(row[col]).strip() not in ['', 'nan']:
                bestellnr = str(row[col]).strip()
                break

        # Match Daten
        if bestellnr in order_map:
            sku = order_map[bestellnr]['SKU']
            titel = order_map[bestellnr]['Angebotstitel']
        else:
            sku = str(row.get('SKU', 'NB /')).strip()
            titel = str(row.get('Angebotstitel', str(row.get('Artikelname', '--')))).strip()

        # Partner / SKU_Prefix bestimmen
        partner = sku.split('/')[0].strip() if '/' in sku else sku.strip()
        if not partner or partner in ['nan', 'None', '']:
            partner = '--'

        # Kundengruppe bestimmen
        is_group_a = any(partner.upper().startswith(p) for p in prefixes_a)
        gruppe = "Gruppe A" if is_group_a else "Gruppe B"

        # Robustere Betragskonvertierung (sucht nach Betrag, Nettobetrag, Amount, etc.)
        betrag_val = 0.0
        for col in row.index:
            c_clean = str(col).lower().replace(' ', '').replace('_', '')
            if any(k in c_clean for k in ['betrag', 'amount', 'erlös', 'netto', 'gesamt']):
                val_str = str(row[col]).strip()
                if val_str and val_str not in ['nan', 'None', '']:
                    clean_str = val_str.replace('€', '').replace(' ', '')
                    if ',' in clean_str and '.' in clean_str:
                        clean_str = clean_str.replace('.', '').replace(',', '.')
                    elif ',' in clean_str:
                        clean_str = clean_str.replace(',', '.')
                    try:
                        betrag_val = float(clean_str)
                        if betrag_val != 0.0:
                            break
                    except ValueError:
                        continue

        datum = str(row.get('Datum der Transaktionserstellung', row.get('Datum', ''))).strip()

        processed.append({
            'Datum': datum,
            'Bestellnummer': bestellnr,
            'Partner': partner,
            'SKU': sku,
            'Angebotstitel': titel,
            'Gruppe': gruppe,
            'Erlös_Brutto': betrag_val,
            'Status': row.get('Status', 'Noch Offen')
        })

    return pd.DataFrame(processed)


def get_group_b_summary(df):
    """Erstellt Tabellenübersicht für Gruppe B."""
    if df.empty:
        return pd.DataFrame()
    
    df_b = df[df['Gruppe'] == 'Gruppe B'].copy()
    if df_b.empty:
        return pd.DataFrame()

    grouped = df_b.groupby('Partner').agg(
        Anzahl_Transaktionen=('Bestellnummer', 'count'),
        eBay_Brutto_Gesamt=('Erlös_Brutto', 'sum')
    ).reset_index()

    grouped['Evelyn_Provision_0_5'] = grouped['eBay_Brutto_Gesamt'] * 0.005
    grouped['Auszahlung_von_Evelyn_an_Dich'] = grouped['eBay_Brutto_Gesamt'] - grouped['Evelyn_Provision_0_5']
    grouped['Deine_Marge_3_0'] = grouped['eBay_Brutto_Gesamt'] * 0.030

    # Gesamtsumme
    total_row = pd.DataFrame([{
        'Partner': 'GESAMTSUMME (Gruppe B)',
        'Anzahl_Transaktionen': grouped['Anzahl_Transaktionen'].sum(),
        'eBay_Brutto_Gesamt': grouped['eBay_Brutto_Gesamt'].sum(),
        'Evelyn_Provision_0_5': grouped['Evelyn_Provision_0_5'].sum(),
        'Auszahlung_von_Evelyn_an_Dich': grouped['Auszahlung_von_Evelyn_an_Dich'].sum(),
        'Deine_Marge_3_0': grouped['Deine_Marge_3_0'].sum()
    }])

    return pd.concat([grouped, total_row], ignore_index=True)


def get_group_a_summary(df):
    """Erstellt Tabellenübersicht für Gruppe A (PP, BA, MK, 001)."""
    if df.empty:
        return pd.DataFrame()
    
    df_a = df[df['Gruppe'] == 'Gruppe A'].copy()
    if df_a.empty:
        return pd.DataFrame()

    grouped = df_a.groupby('Partner').agg(
        Anzahl_Transaktionen=('Bestellnummer', 'count'),
        eBay_Brutto_Gesamt=('Erlös_Brutto', 'sum')
    ).reset_index()

    grouped['Evelyn_Provision_0_5'] = grouped['eBay_Brutto_Gesamt'] * 0.005
    grouped['Direkt_Auszahlung_Evelyn'] = grouped['eBay_Brutto_Gesamt'] - grouped['Evelyn_Provision_0_5']

    total_row = pd.DataFrame([{
        'Partner': 'GESAMTSUMME (Gruppe A)',
        'Anzahl_Transaktionen': grouped['Anzahl_Transaktionen'].sum(),
        'eBay_Brutto_Gesamt': grouped['eBay_Brutto_Gesamt'].sum(),
        'Evelyn_Provision_0_5': grouped['Evelyn_Provision_0_5'].sum(),
        'Direkt_Auszahlung_Evelyn': grouped['Direkt_Auszahlung_Evelyn'].sum()
    }])

    return pd.concat([grouped, total_row], ignore_index=True)


def get_refunds_summary(df):
    """Filtert alle Erstattungen / Gutschriften (negative Beträge)."""
    if df.empty:
        return pd.DataFrame()

    df_ref = df[df['Erlös_Brutto'] < 0].copy()
    if df_ref.empty:
        return pd.DataFrame()

    df_ref['Gutschrift_Brutto'] = df_ref['Erlös_Brutto']
    df_ref['Provision'] = df_ref['Gutschrift_Brutto'] * 0.035
    df_ref['Gutschrift_Netto_Auszahlung'] = df_ref['Gutschrift_Brutto'] - df_ref['Provision']

    return df_ref[['Datum', 'Bestellnummer', 'Partner', 'SKU', 'Angebotstitel', 'Gutschrift_Brutto', 'Provision', 'Gutschrift_Netto_Auszahlung']]


def export_to_excel(df):
    """Erzeugt Excel-Download-Stream ohne externe xlsxwriter-Abhängigkeit."""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Abrechnung')
    return output.getvalue()
