import streamlit as st
import pandas as pd
import sqlite3
import hashlib
import os

# --- PAGE CONFIG ---
st.set_page_config(page_title="eBay Payout & Lexoffice Manager", layout="wide")

DB_FILE = "ebay_data.db"

# --- DATENBANK INITIALISIERUNG ---
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Tabelle für Artikelnamen aus Bestellberichten
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS artikel_db (
            bestellnummer TEXT PRIMARY KEY,
            artikelname TEXT,
            sku TEXT
        )
    """)
    
    # Tabelle für bereits importierte Payout-Dateien (Hash-Schutz)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            file_hash TEXT UNIQUE,
            import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Tabelle für Payout-Transaktionen (Transaktions-ID Schutz)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payouts (
            transaction_id TEXT PRIMARY KEY,
            bestellnummer TEXT,
            sku TEXT,
            artikelname TEXT,
            anzahl REAL,
            netto_betrag REAL,
            datum TEXT,
            payout_file TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# --- HELFERFUNKTIONEN ---
def calculate_file_hash(uploaded_file):
    """Berechnet MD5 Hash des Dateiinhaltes (ignoriert Dateinamen wie (1).csv)"""
    file_bytes = uploaded_file.getvalue()
    return hashlib.md5(file_bytes).hexdigest()

def save_orders_to_db(df):
    """Speichert Artikelnamen & SKUs aus dem Bestellbericht ab"""
    conn = get_db_connection()
    cursor = conn.cursor()
    count = 0
    
    # Mögliche Spaltennamen vereinheitlichen
    col_order = next((c for c in df.columns if 'Bestellnummer' in c or 'Order' in c), None)
    col_title = next((c for c in df.columns if 'Angebotstitel' in c or 'Title' in c or 'Artikel' in c), None)
    col_sku = next((c for c in df.columns if 'Bestandseinheit' in c or 'SKU' in c), None)

    if col_order and col_title:
        for _, row in df.iterrows():
            order_id = str(row[col_order]).strip() if pd.notna(row[col_order]) else None
            title = str(row[col_title]).strip() if pd.notna(row[col_title]) else ""
            sku = str(row[col_sku]).strip() if col_sku and pd.notna(row[col_sku]) else "NB /"
            
            if order_id and order_id != "nan" and title:
                cursor.execute("""
                    INSERT INTO artikel_db (bestellnummer, artikelname, sku)
                    VALUES (?, ?, ?)
                    ON CONFLICT(bestellnummer) DO UPDATE SET
                        artikelname=excluded.artikelname,
                        sku=excluded.sku
                """, (order_id, title, sku))
                count += 1
        conn.commit()
    conn.close()
    return count

def process_payout_files(uploaded_files):
    """Verarbeitet Payouts mit strikter Duplikate-Erkennung"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    messages = []
    
    for file in uploaded_files:
        file_hash = calculate_file_hash(file)
        
        # 1. PRÜFUNG: Wurde exakt dieser Dateiinhalt schon einmal hochgeladen?
        cursor.execute("SELECT filename FROM processed_files WHERE file_hash = ?", (file_hash,))
        already_processed = cursor.fetchone()
        if already_processed:
            messages.append(f"⚠️ **{file.name}**: Dateiinhalt wurde früher bereits importiert (als `{already_processed[0]}`). Übersprungen.")
            continue
            
        # Datei einlesen (unterstützt CSV mit ; oder ,)
        try:
            file.seek(0)
            df = pd.read_csv(file, sep=None, engine='python')
        except Exception as e:
            messages.append(f"❌ **{file.name}**: Fehler beim Lesen ({str(e)})")
            continue
            
        # Spalten ermitteln
        col_tx = next((c for c in df.columns if 'Transaktions' in c or 'Transaction' in c or 'Referenz' in c), None)
        col_order = next((c for c in df.columns if 'Bestellnummer' in c or 'Order' in c), None)
        col_amount = next((c for c in df.columns if 'Netto' in c or 'Betrag' in c or 'Amount' in c), None)
        col_date = next((c for c in df.columns if 'Datum' in c or 'Date' in c), None)
        col_qty = next((c for c in df.columns if 'Anzahl' in c or 'Stück' in c or 'Quantity' in c), None)

        if not col_tx or not col_order:
            # Fallback falls keine echten Transaktions-IDs existieren: Kombinierte ID bauen
            df['generated_tx_id'] = df.index.astype(str) + "_" + df[col_order].astype(str) + "_" + file_hash[:8]
            col_tx = 'generated_tx_id'

        added_tx = 0
        skipped_tx = 0
        
        # Laden der gespeicherten Artikel für Name-Matching
        art_df = pd.read_sql("SELECT * FROM artikel_db", conn)
        art_map = dict(zip(art_df['bestellnummer'], art_df['artikelname']))
        sku_map = dict(zip(art_df['bestellnummer'], art_df['sku']))

        for _, row in df.iterrows():
            tx_id = str(row[col_tx]).strip()
            order_id = str(row[col_order]).strip() if pd.notna(row[col_order]) else ""
            
            # 2. PRÜFUNG: Existiert diese Einzel-Transaktion schon in der DB?
            cursor.execute("SELECT transaction_id FROM payouts WHERE transaction_id = ?", (tx_id,))
            if cursor.fetchone():
                skipped_tx += 1
                continue
                
            amount = float(str(row[amount_col]).replace(',', '.').replace('€', '').strip()) if col_amount and pd.notna(row[col_amount]) else 0.0
            date_val = str(row[col_date]) if col_date and pd.notna(row[col_date]) else ""
            qty = float(row[col_qty]) if col_qty and pd.notna(row[col_qty]) else 1.0
            
            # Name & SKU auflösen
            art_name = art_map.get(order_id, "NB / Kein Titel gefunden")
            sku_val = sku_map.get(order_id, "NB /")

            cursor.execute("""
                INSERT INTO payouts (transaction_id, bestellnummer, sku, artikelname, anzahl, netto_betrag, datum, payout_file)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (tx_id, order_id, sku_val, art_name, qty, amount, date_val, file.name))
            added_tx += 1

        # Datei als verarbeitet markieren
        cursor.execute("INSERT INTO processed_files (filename, file_hash) VALUES (?, ?)", (file.name, file_hash))
        conn.commit()
        
        messages.append(f"✅ **{file.name}**: {added_tx} neue Transaktionen hinzugefügt ({skipped_tx} Duplikate ignoriert).")

    conn.close()
    return messages

# --- SIDEBAR (DATENBANK STATUS & BESTELLBERICHT) ---
with st.sidebar:
    st.header("⚙️ Einstellungen & Datenbank")
    
    st.subheader("📌 Bestellbericht importieren")
    order_file = st.file_uploader("Bestellbericht (CSV/XLSX)", type=["csv", "xlsx"])
    if order_file:
        if order_file.name.endswith('.xlsx'):
            df_ord = pd.read_excel(order_file)
        else:
            try:
                df_ord = pd.read_csv(order_file, sep=';', header=1)
            except:
                df_ord = pd.read_csv(order_file)
        
        imported_count = save_orders_to_db(df_ord)
        st.success(f"{imported_count} Artikelbezeichnungen erfolgreich in DB gesichert!")

    st.divider()
    
    # DB Status Abfragen
    conn = get_db_connection()
    total_articles = pd.read_sql("SELECT COUNT(*) as c FROM artikel_db", conn)['c'][0]
    total_payouts = pd.read_sql("SELECT COUNT(*) as c FROM payouts", conn)['c'][0]
    conn.close()
    
    st.subheader("📦 DB-Status")
    st.write(f"* **Gespeicherte Titel/Artikel:** {total_articles}")
    st.write(f"* **Verarbeitete Payout-Zeilen:** {total_payouts}")
    
    if st.button("🗑️ Datenbank leeren (Reset)"):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM artikel_db")
        cursor.execute("DELETE FROM processed_files")
        cursor.execute("DELETE FROM payouts")
        conn.commit()
        conn.close()
        st.experimental_rerun()

# --- HAUPTBEREICH ---
st.title("⚡ eBay Payout & Lexoffice Direct-Upload")

st.subheader("1. eBay Auszahlungsberichte hochladen (CSV)")
payout_files = st.file_uploader("Payouts hochladen", type=["csv"], accept_multiple_files=True)

if payout_files:
    results = process_payout_files(payout_files)
    for msg in results:
        st.info(msg)

# --- TABELLEN-ANZEIGE DER GESPEICHERTEN DATEN ---
conn = get_db_connection()
df_payouts_db = pd.read_sql("SELECT * FROM payouts", conn)
conn.close()

if not df_payouts_db.empty:
    st.divider()
    st.subheader("📊 Übersicht aller Transaktionen (Dauerhaft gespeichert)")
    
    total_sum = df_payouts_db['netto_betrag'].sum()
    st.write(f"**Anzahl Gesamt:** {len(df_payouts_db)} Positionen | **Gesamtsumme Netto:** {total_sum:.2f} €")
    
    # Anzeige-Tabelle aufbereiten
    df_display = df_payouts_db[['bestellnummer', 'sku', 'artikelname', 'anzahl', 'netto_betrag', 'datum']].copy()
    df_display.columns = ['Bestellnummer', 'SKU', 'Artikelname', 'Stück', 'eBay_Netto', 'Datum der Transaktionserstellung']
    
    st.dataframe(df_display, use_container_width=True)
else:
    st.warning("Noch keine Payouts in der Datenbank vorhanden. Bitte oben CSVs hochladen.")
