import os
import pandas as pd

ORDERS_DB_PATH = "Master_Orders.csv"
PAYOUTS_DB_PATH = "Master_Payouts.csv"

def save_and_merge_order_reports(uploaded_files, upload_folder="uploads"):
    """
    Speichert und konsolidiert hochgeladene Bestellberichte in Master_Orders.csv
    """
    os.makedirs(upload_folder, exist_ok=True)
    all_order_data = []
    
    for file in uploaded_files:
        filepath = os.path.join(upload_folder, file.name)
        with open(filepath, "wb") as f:
            f.write(file.getbuffer())
            
        try:
            # Unterstützung für CSV (verschiedene Trennzeichen) und Excel
            if file.name.lower().endswith(('.xlsx', '.xls')):
                df = pd.read_excel(filepath, dtype=str)
            else:
                df = pd.read_csv(filepath, sep=None, engine='python', dtype=str)
                
            df.columns = [str(c).strip() for c in df.columns]
            all_order_data.append(df)
        except Exception:
            pass
        
    if all_order_data:
        merged_orders = pd.concat(all_order_data, ignore_index=True)
        merged_orders.drop_duplicates(inplace=True)
        merged_orders.to_csv(ORDERS_DB_PATH, sep=';', index=False, encoding='utf-8-sig')
        return merged_orders
    elif os.path.exists(ORDERS_DB_PATH):
        return pd.read_csv(ORDERS_DB_PATH, sep=';', dtype=str)
        
    return pd.DataFrame()


def categorize_sku(sku):
    """
    Ordet eine SKU der passenden Gruppe zu (A, B oder Verkäufergruppe)
    """
    if not sku or pd.isna(sku) or str(sku).strip() in ['', 'NB /', 'nan']:
        return "Unbekannt / Ohne Zuordnung"
    
    sku_upper = str(sku).strip().upper()
    
    # Beispielhafte Präfix-Logik (kann nach deinen Wünschen angepasst werden)
    if sku_upper.startswith("A-") or sku_upper.startswith("A_") or "GRP-A" in sku_upper:
        return "Kundengruppe A"
    elif sku_upper.startswith("B-") or sku_upper.startswith("B_") or "GRP-B" in sku_upper:
        return "Kundengruppe B"
    else:
        return "Verkäufergruppe / Standard"


def build_transaction_overview(master_payout_path=PAYOUTS_DB_PATH, master_orders_path=ORDERS_DB_PATH):
    """
    Baut die vollständige Übersicht mit Bestellbericht-Verknüpfung
    und Kundengruppen-Kategorisierung auf.
    """
    if not os.path.exists(master_payout_path):
        return pd.DataFrame(), 0, 0.0

    try:
        payouts = pd.read_csv(master_payout_path, sep=';', dtype=str)
    except Exception:
        return pd.DataFrame(), 0, 0.0

    if payouts.empty:
        return pd.DataFrame(), 0, 0.0

    # Bestellberichte laden
    orders = pd.DataFrame()
    if os.path.exists(master_orders_path):
        try:
            orders = pd.read_csv(master_orders_path, sep=';', dtype=str)
        except Exception:
            orders = pd.DataFrame()

    # Mapping-Table für Artikel und SKUs aus den 188 DB-Einträgen aufbauen
    order_map = {}
    if not orders.empty:
        cols_lower = {str(c).lower().strip(): c for c in orders.columns}
        
        # Flexibles Erkennen aller gängigen Spaltenbezeichnungen
        order_id_col = next((cols_lower[k] for k in cols_lower if k in [
            'bestellnummer', 'order id', 'order number', 'verkaufsnummer', 'verkaufsprotokoll-nr.', 'transaktions-id', 'sales record number'
        ]), None)
        
        sku_col = next((cols_lower[k] for k in cols_lower if k in [
            'sku', 'custom label', 'eigene sku', 'bestandseinheit', 'angebotsnummer', 'item number', 'artikelnummer'
        ]), None)
        
        title_col = next((cols_lower[k] for k in cols_lower if k in [
            'artikelname', 'artikeltitel', 'item title', 'artikelbezeichnung', 'title', 'bezeichnung'
        ]), None)

        if order_id_col:
            for _, row in orders.iterrows():
                oid = str(row[order_id_col]).strip()
                if oid and oid != 'nan':
                    order_map[oid] = {
                        'SKU': str(row[sku_col]).strip() if sku_col and pd.notna(row[sku_col]) else 'NB /',
                        'Artikelname': str(row[title_col]).strip() if title_col and pd.notna(row[title_col]) else 'NB / Kein Titel gefunden'
                    }

    result_rows = []
    total_netto = 0.0

    for _, row in payouts.iterrows():
        # Bestellnummer aus Payout suchen
        bestellnr = None
        for col in ['Bestellnummer', 'Order ID', 'Verkaufsnummer', 'Auszahlung Nr.']:
            if col in row and pd.notna(row[col]) and str(row[col]).strip() not in ['', 'nan']:
                bestellnr = str(row[col]).strip()
                break
        
        if not bestellnr:
            bestellnr = '--'

        # Artikel & SKU matchen
        if bestellnr in order_map:
            sku = order_map[bestellnr]['SKU']
            artikelname = order_map[bestellnr]['Artikelname']
        else:
            sku = 'NB /'
            artikelname = 'NB / Kein Titel gefunden'

        # Kategorie bestimmen
        gruppe = categorize_sku(sku)

        # Stückzahl
        stueck = '1'
        for col in ['Menge', 'Stückzahl', 'Anzahl', 'Quantity']:
            if col in row and pd.notna(row[col]) and str(row[col]).strip() not in ['', 'nan', '--']:
                stueck = str(row[col]).strip()
                break

        # Nettobetrag ermitteln
        betrag_val = 0.0
        for col in ['Nettobetrag', 'Betrag', 'Gesamtbetrag', 'Amount']:
            if col in row and pd.notna(row[col]):
                raw_v = str(row[col]).replace('.', '').replace(',', '.') if ',' in str(row[col]) else str(row[col])
                try:
                    betrag_val = float(raw_v)
                    break
                except ValueError:
                    continue

        total_netto += betrag_val
        datum = str(row.get('Datum der Transaktionserstellung', row.get('Datum', ''))).strip()

        result_rows.append({
            'Bestellnummer': bestellnr,
            'SKU': sku,
            'Artikelname': artikelname,
            'Gruppe': gruppe,
            'Stück': stueck,
            'eBay_Netto': f"{betrag_val:.2f}".replace('.', ','),
            'Datum der Transaktionserstellung': datum
        })

    result_df = pd.DataFrame(result_rows)
    return result_df, len(orders), total_netto
