import io
import pandas as pd

def parse_ebay_payout_csv(file_bytes):
    """
    Liest den Inhalt einer hochgeladenen Datei ein, 
    filtert den eBay-Vorspann und gibt einen DataFrame zurück.
    """
    content = file_bytes.decode('utf-8', errors='ignore')
    lines = content.splitlines()
    
    header_idx = None
    for idx, line in enumerate(lines):
        if 'Datum der Transaktionserstellung' in line and 'Auszahlung Nr.' in line:
            header_idx = idx
            break
            
    if header_idx is None:
        return None
    
    valid_lines = [lines[header_idx]] + [l for l in lines[header_idx+1:] if ';' in l]
    csv_data = "\n".join(valid_lines)
    
    df = pd.read_csv(io.StringIO(csv_data), sep=';', quotechar='"', dtype=str)
    df.columns = [c.strip() for c in df.columns]
    return df


def process_payout_uploads(uploaded_files, existing_master_df=None):
    """
    Verarbeitet mehrere hochgeladene Dateien, filtert Dubletten anhand der 'Auszahlung Nr.'
    und gibt die neuen Daten sowie Log-Meldungen zurück.
    """
    existing_payout_ids = set()
    if existing_master_df is not None and not existing_master_df.empty:
        if 'Auszahlung Nr.' in existing_master_df.columns:
            existing_payout_ids = set(existing_master_df['Auszahlung Nr.'].dropna().unique())

    new_dfs = []
    logs = []

    for uploaded_file in uploaded_files:
        file_bytes = uploaded_file.getvalue()
        df = parse_ebay_payout_csv(file_bytes)
        
        if df is None or df.empty or 'Auszahlung Nr.' not in df.columns:
            logs.append(f"⚠️ Ungültiges Format: '{uploaded_file.name}'")
            continue
            
        payout_id = df['Auszahlung Nr.'].iloc[0]
        
        # DUBLETTEN-PRÜFUNG
        if payout_id in existing_payout_ids:
            logs.append(f"⛔ Dublette übersprungen: Auszahlung {payout_id} ({uploaded_file.name})")
        else:
            logs.append(f"✅ Importiert: Auszahlung {payout_id} ({uploaded_file.name})")
            existing_payout_ids.add(payout_id)
            new_dfs.append(df)

    if new_dfs:
        combined_new_df = pd.concat(new_dfs, ignore_index=True)
        return combined_new_df, logs
    
    return pd.DataFrame(), logs
