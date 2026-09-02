import os
import pandas as pd

ORDERS_DB_PATH = "Master_Orders.csv"
PAYOUTS_DB_PATH = "Master_Payouts.csv"

def categorize_sku(sku):
    if not sku or pd.isna(sku) or str(sku).strip() in ['', 'NB /', 'nan', 'None']:
        return "Kundengruppe B"
    
    s = str(sku).strip().upper()
    prefixes_a = ["PP", "BA", "MK", "001"]
    if any(s.startswith(p) for p in prefixes_a):
        return "Kundengruppe A (Evelyn)"
    
    return "Kundengruppe B"


def build_transaction_overview(master_payout_path=PAYOUTS_DB_PATH, master_orders_path=ORDERS_DB_PATH):
    if not os.path.exists(master_payout_path):
        return pd.DataFrame(), 0, 0.0

    try:
        payouts = pd.read_csv(master_payout_path, sep=';', dtype=str)
    except Exception:
        return pd.DataFrame(), 0, 0.0

    if payouts.empty:
        return pd.DataFrame(), 0, 0.0

    orders = pd.DataFrame()
    if os.path.exists(master_orders_path):
        try:
            orders = pd.read_csv(master_orders_path, sep=';', dtype=str)
        except Exception:
            orders = pd.DataFrame()

    # Mapping aus den Bestellberichten
    order_map = {}
    if not orders.empty:
        cols_norm = {str(c).lower().replace('-', '').replace('_', '').replace(' ', ''): c for c in orders.columns}
        
        oid_col = next((cols_norm[k] for k in ['bestellnummer', 'orderid', 'ordernumber', 'verkaufsnummer', 'verkaufsprotokollnr', 'transaktionsid'] if k in cols_norm), None)
        sku_col = next((cols_norm[k] for k in ['sku', 'customlabel', 'eigenesku', 'bestandseinheit', 'angebotsnummer', 'artikelnummer'] if k in cols_norm), None)
        title_col = next((cols_norm[k] for k in ['artikelname', 'artikeltitel', 'itemtitle', 'artikelbezeichnung', 'title'] if k in cols_norm), None)

        if oid_col:
            for _, row in orders.iterrows():
                oid = str(row[oid_col]).strip()
                if oid and oid not in ['nan', 'None', '']:
                    sku_v = str(row[sku_col]).strip() if sku_col and pd.notna(row[sku_col]) else 'NB /'
                    title_v = str(row[title_col]).strip() if title_col and pd.notna(row[title_col]) else 'NB / Kein Titel gefunden'
                    order_map[oid] = {'SKU': sku_v, 'Artikelname': title_v}

    result_rows = []
    total_netto = 0.0

    for _, row in payouts.iterrows():
        bestellnr = '--'
        for col in ['Bestellnummer', 'Order ID', 'Verkaufsnummer']:
            if col in row and pd.notna(row[col]) and str(row[col]).strip() not in ['', 'nan']:
                bestellnr = str(row[col]).strip()
                break

        if bestellnr in order_map:
            sku = order_map[bestellnr]['SKU']
            artikelname = order_map[bestellnr]['Artikelname']
        else:
            sku = 'NB /'
            artikelname = 'NB / Kein Titel gefunden'

        gruppe = categorize_sku(sku)

        # Netto-Betrag auslesen
        betrag_val = 0.0
        for col in ['Nettobetrag', 'Betrag', 'Gesamtbetrag', 'Amount']:
            if col in row and pd.notna(row[col]):
                raw_s = str(row[col]).strip().replace('.', '').replace(',', '.') if ',' in str(row[col]) else str(row[col]).strip()
                try:
                    betrag_val = float(raw_s)
                    break
                except ValueError:
                    continue

        total_netto += betrag_val

        # Berechnungen je nach Kundengruppe
        if "Gruppe A" in gruppe:
            # Evelyn: Direct + 0.5% Provision/Rabatt
            evelyn_netto = betrag_val * 0.995
            partner_netto = 0.0
            lexoffice_netto = evelyn_netto
        else:
            # Gruppe B: Lexoffice 0.5% Rabatt, Partner 3.5% Rabatt
            evelyn_netto = 0.0
            lexoffice_netto = betrag_val * 0.995
            partner_netto = betrag_val * 0.965

        result_rows.append({
            'Bestellnummer': bestellnr,
            'SKU': sku,
            'Artikelname': artikelname,
            'Gruppe': gruppe,
            'eBay_Netto': f"{betrag_val:.2f}".replace('.', ','),
            'Evelyn_Netto (-0.5%)': f"{evelyn_netto:.2f}".replace('.', ',') if evelyn_netto > 0 else '--',
            'Lexoffice_Netto (-0.5%)': f"{lexoffice_netto:.2f}".replace('.', ','),
            'Partner_Netto (-3.5%)': f"{partner_netto:.2f}".replace('.', ',') if partner_netto > 0 else '--',
            'Datum': str(row.get('Datum der Transaktionserstellung', row.get('Datum', ''))).strip()
        })

    return pd.DataFrame(result_rows), len(orders), total_netto
