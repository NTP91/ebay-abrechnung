import os
import glob
import io
import pandas as pd

def parse_ebay_payout_csv(filepath):
    """
    Liest eine eBay-Payout-CSV-Datei ein, erkennt die Header-Zeile 
    und filtert Datenzeilen heraus.
    """
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    
    header_idx = None
    for idx, line in enumerate(lines):
        if 'Datum der Transaktionserstellung' in line and 'Auszahlung Nr.' in line:
            header_idx = idx
            break
            
    if header_idx is None:
        raise ValueError(f"Keinen gültigen Auszahlungs-Header in '{filepath}' gefunden.")
    
    valid_lines = [lines[header_idx]]
    for l in lines[header_idx+1:]:
        if ';' in l:
            valid_lines.append(l)
            
    csv_data = "".join(valid_lines)
    df = pd.read_csv(io.StringIO(csv_data), sep=';', quotechar='"', dtype=str)
    df.columns = [c.strip() for c in df.columns]
    return df


def import_payout_files(input_directory="uploads", output_master_csv="Master_Payouts.csv"):
    """
    Importiert alle Payout-CSV-Dateien aus input_directory, schützt vor Dubletten
    und speichert die konsolidierten Daten in output_master_csv.
    """
    imported_payout_ids = set()
    
    if os.path.exists(output_master_csv):
        try:
            existing_master_df = pd.read_csv(output_master_csv, sep=';', dtype=str)
            if 'Auszahlung Nr.' in existing_master_df.columns:
                imported_payout_ids = set(existing_master_df['Auszahlung Nr.'].dropna().unique())
        except Exception:
            existing_master_df = pd.DataFrame()
    else:
        existing_master_df = pd.DataFrame()

    csv_files = glob.glob(os.path.join(input_directory, "*.csv"))
    csv_files = [f for f in csv_files if os.path.basename(f) != os.path.basename(output_master_csv)]

    if not csv_files:
        return

    new_dfs = []
    
    for filepath in sorted(csv_files):
        filename = os.path.basename(filepath)
        try:
            df = parse_ebay_payout_csv(filepath)
            if df.empty or 'Auszahlung Nr.' not in df.columns:
                continue
            
            payout_id = df['Auszahlung Nr.'].iloc[0]
            
            if payout_id not in imported_payout_ids:
                imported_payout_ids.add(payout_id)
                new_dfs.append(df)
                
        except Exception:
            pass

    if new_dfs:
        all_new_data = pd.concat(new_dfs, ignore_index=True)
        if not existing_master_df.empty:
            final_master_df = pd.concat([existing_master_df, all_new_data], ignore_index=True)
        else:
            final_master_df = all_new_data
            
        final_master_df.to_csv(output_master_csv, sep=';', index=False, encoding='utf-8-sig')
