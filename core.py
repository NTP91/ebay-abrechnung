import os
import glob
import pandas as pd

ORDERS_DB_PATH = "Master_Orders.csv"

def save_and_merge_order_reports(uploaded_files, upload_folder="uploads"):
    """
    Nimmt CSV & XLSX Bestellberichte entgegen, zieht SKU und Artikelname heraus
    und baut/aktualisiert die Master_Orders.csv.
    """
    all_order_data = []
    
    for file in uploaded_files:
        filepath = os.path.join(upload_folder, file.name)
        with open(filepath, "wb") as f:
            f.write(file.getbuffer())
            
        if file.name.endswith('.xlsx') or file.name.endswith('.xls'):
            df = pd.read_excel(filepath, dtype=str)
        elif file.name.endswith('.csv'):
            df = pd.read_csv(filepath, sep=None, engine='python', dtype=str)
        else:
            continue
            
        df.columns = [str(c).strip() for c in df.columns]
        all_order_data.append(df)
        
    if all_order_data:
        merged_orders = pd.concat(all_order_data, ignore_index=True)
        merged_orders.drop_duplicates(inplace=True)
        merged_orders.to_csv(ORDERS_DB_PATH, sep=';', index=False, encoding='utf-8-sig')
        return merged_orders
    elif os.path.exists(ORDERS_DB_PATH):
        return pd.read_csv(ORDERS_DB_PATH, sep=';', dtype=str)
        
    return pd.DataFrame()


def build_transaction_overview(master_payout_path="Master_Payouts.csv", master_orders_path="Master_Orders.csv"):
    """
    Verknüpft Payouts mit den Bestellberichten über die Bestellnummer,
    um SKU, Artikelname und Netto-Beträge zuzuordnen.
    """
    if not os.path.exists(master_payout_path):
        return pd.DataFrame(), 0, 0.0

    payouts = pd.read_csv(master_payout_path, sep=';', dtype=str)
    if payouts.empty:
        return pd.DataFrame(), 0, 0.0

    orders = pd.DataFrame()
    if os.path.exists(master_orders_path):
        try:
            orders = pd.read_csv(master_orders_path, sep=';', dtype=str)
        except Exception:
            orders = pd.DataFrame()

    # Mapping-Schilder aufbauen
    order_map = {}
    if not orders.empty:
        # Finde Spaltennamen flexibel
        order_id_col = next((c for c in ['Bestellnummer', 'Order ID', 'Verkaufsnummer'] if c in orders.columns), None)
        sku_col = next((c for c in ['SKU', 'Angebotsnummer', 'Custom Label'] if c in orders.columns), None)
        title_col = next((c for c in ['Artikelname', 'Artikeltitel', 'Item Title'] if c in orders.columns), None)

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
        bestellnr = str(row.get('Bestellnummer', '')).strip()
        
        # Mapping anwenden
        if bestellnr in order_map:
            sku = order_map[bestellnr]['SKU']
            artikelname = order_map[bestellnr]['Artikelname']
        else:
            sku = 'NB /'
            artikelname = 'NB / Kein Titel gefunden'

        # Stückzahl ermitteln
        stueck = str(row.get('Menge', row.get('Stückzahl', '1'))).strip()
        if not stueck or stueck == 'nan':
            stueck = '1'

        # Betrag / Netto ermitteln
        betrag_raw = str(row.get('Nettobetrag', row.get('Betrag', '0'))).replace(',', '.')
        try:
            betrag_val = float(betrag_raw)
        except ValueError:
            betrag_val = 0.0

        total_netto += betrag_val

        datum = str(row.get('Datum der Transaktionserstellung', '')).strip()

        result_rows.append({
            'Bestellnummer': bestellnr,
            'SKU': sku,
            'Artikelname': artikelname,
            'Stück': stueck,
            'eBay_Netto': f"{betrag_val:.2f}".replace('.', ','),
            'Datum der Transaktionserstellung': datum
        })

    result_df = pd.DataFrame(result_rows)
    return result_df, len(orders), total_netto
