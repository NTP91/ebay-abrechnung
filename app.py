import streamlit as st
import pandas as pd
import sqlite3
import hashlib
import io

# --- PAGE CONFIG ---
st.set_page_config(page_title="eBay Payout & Lexoffice Manager", layout="wide")

DB_FILE = "ebay_data.db"

# --- DATENBANK INITIALISIERUNG ---
def get_db_connection():
    return sqlite3.connect(DB_FILE)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Artikel-Datenbank (Bestellberichte August)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS artikel_db (
            bestellnummer TEXT PRIMARY KEY,
            artikelname TEXT,
            sku TEXT
        )
    """)
    
    # 2. Payout-Dateien Duplikatschutz
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            file_hash TEXT UNIQUE,
            import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 3. Payouts Tabelle
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
    file_bytes = uploaded_file.getvalue()
    return hashlib.md5(file_bytes).hexdigest()

def read_payout_csv_safely(uploaded_file):
    """Liest eBay Payout CSVs extrem robust ein (fängt Anführungszeichen-Syntaxfehler ab)"""
    content = uploaded_file.getvalue()
    
    # Versuche verschiedene Encodings und Trennzeichen
    for encoding in ['utf-8', 'latin1', 'cp1252']:
        for sep in [',', ';', '\t']:
            try:
                uploaded_file.seek(0)
                df = pd.read_csv(
                    io.BytesIO(content),
                    sep=sep,
                    encoding=encoding,
                    on_bad_lines='skip',
                    quotechar='"'
                )
                if len(df.columns) > 1:
                    return df
            except Exception:
                continue
    # Fallback mit Standard read_csv
    uploaded_file.seek(0)
    return pd.read_csv(uploaded_file, on_bad_lines='skip')

def normalize_dataframe(df):
    """Sucht automatisch die Kopfzeile mit 'Bestellnummer'"""
    if not any('Bestellnummer' in str(col) for col in df.columns):
        for idx, row in df.iterrows():
            if row.astype(str).str.contains('Bestellnummer').any():
                df.columns = df.iloc[idx]
                df = df.iloc[idx+1:].reset_index(drop=True)
                break
    return df

def save_single_order_file(uploaded_file):
    """Verarbeitet eine einzelne Bestellbericht-Datei (CSV oder XLSX)"""
    try:
        uploaded_file.seek(0)
        if uploaded_file.name.lower().endswith('.xlsx'):
            df = pd.read_excel(uploaded_file, engine='openpyxl')
        else:
            try:
                df = pd.read_csv(uploaded_file, sep=';', header=1)
            except:
                uploaded_file.seek(0)
                df = pd.read_csv(uploaded_file, sep=None, engine='python', on_bad_lines='skip')

        df = normalize_dataframe(df)

        col_order = next((c for c in df.columns if 'Bestellnummer' in str(c)), None)
        col_title = next((c for c in df.columns if 'Angebotstitel' in str(c) or 'Title' in str(c)), None)
        col_sku = next((c for c in df.columns if 'Bestandseinheit' in str(c) or 'SKU' in str(c)), None)

        if not col_order or not col_title:
            return 0, f"❌ `{uploaded_file.name}`: Spalten 'Bestellnummer'/'Angebotstitel' nicht gefunden."

        conn = get_db_connection()
        cursor = conn.cursor()
        count = 0
        
        for _, row in df.iterrows():
            order_id = str(row[col_order]).strip() if pd.notna(row[col_order]) else None
            title = str(row[col_title]).strip() if pd.notna(row[col_title]) else ""
            sku = str(row[col_sku]).strip() if col_sku and pd.notna(row[col_sku]) else "NB /"
            
            if order_id and order_id != "nan" and title and title != "nan":
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
        return count, f"✅ `{uploaded_file.name}`: {count} Artikel verarbeitet."
    except Exception as e:
        return 0, f"❌ Fehler bei `{uploaded_file.name}`: {str(e)}"

def process_payout_files(uploaded_files):
    """Verarbeitet Payout-CSVs ohne Duplikate"""
    conn = get_db_connection()
    cursor = conn.cursor()
    messages = []
    
    for file in uploaded_files:
        file_hash = calculate_file_hash(file)
        
        cursor.execute("SELECT filename FROM processed_files WHERE file_hash = ?", (file_hash,))
        already_processed = cursor.fetchone()
        if already_processed:
            messages.append(f"⚠️ **{file.name}**: Wurde früher schon importiert (`{already_processed[0]}`). Übersprungen.")
            continue
            
        file.seek(0)
        try:
            df = read_payout_csv_safely(file)
        except Exception as e:
            messages.append(f"❌ **{file.name}**: Fehler beim Einlesen ({str(e)})")
            continue
            
        col_tx = next((c for c in df.columns if 'Transaktions' in str(c) or 'Transaction' in str(c) or 'Referenz' in str(c)), None)
        col_order = next((c for c in df.columns if 'Bestellnummer' in str(c) or 'Order' in str(c)), None)
        col_amount = next((c for c in df.columns if 'Netto' in str(c) or 'Betrag' in str(c) or 'Amount' in str(c)), None)
        col_date = next((c for c in df.columns if 'Datum' in str(c) or 'Date' in str(c)), None)
        col_qty = next((c for c in df.columns if 'Anzahl' in str(c) or 'Stück' in str(c) or 'Quantity' in str(c)), None)

        if not col_tx and col_order:
            df['generated_tx_id'] = df.index.astype(str) + "_" + df[col_order].astype(str) + "_" + file_hash[:6]
            col_tx = 'generated_tx_id'

        added_tx = 0
        skipped_tx = 0
        
        art_df = pd.read_sql("SELECT * FROM artikel_db", conn)
        art_map = dict(zip(art_df['bestellnummer'], art_df['artikelname']))
        sku_map = dict(zip(art_df['bestellnummer'], art_df['sku']))

        if col_order:
            for _, row in df.iterrows():
                tx_id = str(row[col_tx]).strip() if col_tx else hashlib.md5(str(row).encode()).hexdigest()
                order_id = str(row[col_order]).strip() if pd.notna(row[col_order]) else ""
                
                cursor.execute("SELECT transaction_id FROM payouts WHERE transaction_id = ?", (tx_id,))
                if cursor.fetchone():
                    skipped_tx += 1
                    continue
                    
                amount = float(str(row[col_amount]).replace(',', '.').replace('€', '').strip()) if col_amount and pd.notna(row[col_amount]) else 0.0
                date_val = str(row[col_date]) if col_date and pd.notna(row[col_date]) else ""
                qty = float(row[col_qty]) if col_qty and pd.notna(row[col_qty]) else 1.0
                
                art_name = art_map.get(order_id, "NB / Kein Titel gefunden")
                sku_val = sku_map.get(order_id, "NB /")

                cursor.execute("""
                    INSERT INTO payouts (transaction_id, bestellnummer, sku, artikelname, anzahl, netto_betrag, datum, payout_file)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (tx_id, order_id, sku_val, art_name, qty, amount, date_val, file.name))
                added_tx += 1

            cursor.execute("INSERT INTO processed_files (filename, file_hash) VALUES (?, ?)", (file.name, file_hash))
            conn.commit()
            messages.append(f"✅ **{file.name}**: {added_tx} neue Zeilen importiert ({skipped_tx} Duplikate geblockt).")
        else:
            messages.append(f"⚠️ **{file.name}**: Keinen verknüpfbaren Inhalt/Spalten gefunden.")

    conn.close()
    return messages

# --- SIDEBAR (DATENBANK STATUS & BESTELLBERICHTE) ---
with st.sidebar:
    st.header("⚙️ Einstellungen & Datenbank")
    
    st.subheader("📌 Bestellberichte importieren")
    st.caption("Wähle hier beide Dateien (CSV & XLSX) gleichzeitig aus:")
    
    order_files = st.file_uploader("Bestellberichte (CSV & XLSX)", type=["csv", "xlsx"], accept_multiple_files=True, key="orders_up")
    
    if order_files:
        total_imp = 0
        for f in order_files:
            c, msg = save_single_order_file(f)
            total_imp += c
            if c > 0:
                st.success(msg)
            else:
                st.error(msg)

    st.divider()
    
    # DB Status
    conn = get_db_connection()
    total_articles = pd.read_sql("SELECT COUNT(*) as c FROM artikel_db", conn)['c'][0]
    total_payouts = pd.read_sql("SELECT COUNT(*) as c FROM payouts", conn)['c'][0]
    conn.close()
    
    st.subheader("📦 DB-Status")
    st.write(f"* **Artikel in DB:** `{total_articles}`")
    st.write(f"* **Gesp. Payout-Zeilen:** `{total_payouts}`")
    
    if st.button("🗑️ Datenbank komplett leeren"):
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
payout_files = st.file_uploader("Payouts hochladen", type=["csv"], accept_multiple_files=True, key="payouts_up")

if payout_files:
    results = process_payout_files(payout_files)
    for msg in results:
        st.info(msg)

# --- ANZEIGE DER DATEN ---
conn = get_db_connection()
df_payouts_db = pd.read_sql("SELECT * FROM payouts", conn)
conn.close()

if not df_payouts_db.empty:
    st.divider()
    st.subheader("📊 Übersicht aller Transaktionen (Dauerhaft in DB)")
    
    total_sum = df_payouts_db['netto_betrag'].sum()
    st.write(f"**Anzahl Gesamt:** {len(df_payouts_db)} Positionen | **Gesamtsumme Netto:** {total_sum:.2f} €")
    
    df_display = df_payouts_db[['bestellnummer', 'sku', 'artikelname', 'anzahl', 'netto_betrag', 'datum']].copy()
    df_display.columns = ['Bestellnummer', 'SKU', 'Artikelname', 'Stück', 'eBay_Netto', 'Datum der Transaktionserstellung']
    
    st.dataframe(df_display, use_container_width=True)
else:
    st.warning("Noch keine Payouts in der Datenbank vorhanden.")
