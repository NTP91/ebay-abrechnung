import os
import io
import glob
import pandas as pd

def parse_ebay_payout_csv(filepath):
    """
    Sucht nach der Header-Zeile in der eBay-Payout-CSV,
    ignoriert den Vorspann und gibt ein sauberes DataFrame zurück.
    """
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    
    header_idx = None
    for idx, line in enumerate(lines):
        if 'Datum der Transaktionserstellung' in line and 'Auszahlung Nr.' in line:
            header_idx = idx
            break
            
    if header_idx is None:
        return pd.DataFrame()
    
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
    Liest Payout-CSVs ein, prüft anhand von 'Auszahlung Nr.' auf Dubletten
    und speichert neue Einträge in die Master_Payouts.csv.
    """
    imported_payout_ids = set()
    existing_master_df = pd.DataFrame()
    
    if os.path.exists(output_master_csv):
        try:
            existing_master_df = pd.read_csv(output_master_csv, sep=';', dtype=str)
            if 'Auszahlung Nr.' in existing_master_df.columns:
                imported_payout_ids = set(existing_master_df['Auszahlung Nr.'].dropna().unique())
        except Exception:
            existing_master_df = pd.DataFrame()

    csv_files = glob.glob(os.path.join(input_directory, "*.csv"))
    csv_files = [f for f in csv_files if os.path.basename(f) != os.path.basename(output_master_csv)]

    if not csv_files:
        return existing_master_df, []

    new_dfs = []
    logs = []
    
    for filepath in sorted(csv_files):
        filename = os.path.basename(filepath)
        df = parse_ebay_payout_csv(filepath)
        
        if df.empty or 'Auszahlung Nr.' not in df.columns:
            continue
            
        payout_id = df['Auszahlung Nr.'].iloc[0]
        
        if payout_id in imported_payout_ids:
            logs.append(f"⚠️ {filename}: Wurde früher schon importiert ({payout_id}). Übersprungen.")
        else:
            imported_payout_ids.add(payout_id)
            new_dfs.append(df)
            logs.append(f"✅ {filename}: Erfogreich importiert ({payout_id}).")

    if new_dfs:
        all_new_data = pd.concat(new_dfs, ignore_index=True)
        if not existing_master_df.empty:
            final_master_df = pd.concat([existing_master_df, all_new_data], ignore_index=True)
        else:
            final_master_df = all_new_data
            
        final_master_df.to_csv(output_master_csv, sep=';', index=False, encoding='utf-8-sig')
        return final_master_df, logs
        
    return existing_master_df, logs
