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
    
    # Header-Zeile suchen
    header_idx = None
    for idx, line in enumerate(lines):
        if 'Datum der Transaktionserstellung' in line and 'Auszahlung Nr.' in line:
            header_idx = idx
            break
            
    if header_idx is None:
        raise ValueError(f"Keinen gültigen Auszahlungs-Header in '{filepath}' gefunden.")
    
    # Nur gültige Datenzeilen mit Semikolon übernehmen
    valid_lines = [lines[header_idx]]
    for l in lines[header_idx+1:]:
        if ';' in l:
            valid_lines.append(l)
            
    csv_data = "".join(valid_lines)
    df = pd.read_csv(io.StringIO(csv_data), sep=';', quotechar='"', dtype=str)
    
    # Spaltennamen säubern
    df.columns = [c.strip() for c in df.columns]
    return df


def import_payout_files(input_directory=".", output_master_csv="Master_Payouts.csv"):
    """
    Importiert alle Payout-CSV-Dateien aus input_directory, schützt vor Dubletten
    und speichert die konsolidierten Daten in output_master_csv.
    """
    imported_payout_ids = set()
    
    # 1. Bisherige Master-Datei laden (falls vorhanden), um historische Dubletten zu vermeiden
    if os.path.exists(output_master_csv):
        existing_master_df = pd.read_csv(output_master_csv, sep=';', dtype=str)
        if 'Auszahlung Nr.' in existing_master_df.columns:
            imported_payout_ids = set(existing_master_df['Auszahlung Nr.'].dropna().unique())
            print(f"ℹ️ Bereits gespeicherte Auszahlungen in Master-Datei: {len(imported_payout_ids)}")
    else:
        existing_master_df = pd.DataFrame()

    # 2. Alle CSV-Dateien im Ordner suchen (außer die Ziel-Datei selbst)
    csv_files = glob.glob(os.path.join(input_directory, "*.csv"))
    csv_files = [f for f in csv_files if os.path.basename(f) != os.path.basename(output_master_csv)]

    if not csv_files:
        print("⚠️ Keine CSV-Dateien im Ordner gefunden.")
        return

    new_dfs = []
    
    # 3. Dateien nacheinander verarbeiten & auf Dubletten prüfen
    for filepath in sorted(csv_files):
        filename = os.path.basename(filepath)
        try:
            df = parse_ebay_payout_csv(filepath)
            
            if df.empty or 'Auszahlung Nr.' not in df.columns:
                continue
            
            payout_id = df['Auszahlung Nr.'].iloc[0]
            
            # --- DUBLETTEN-PRÜFUNG ---
            if payout_id in imported_payout_ids:
                print(f"⛔ DUBLETTE IGNORE: '{filename}' (Auszahlung Nr. {payout_id}) ist bereits erfasst.")
            else:
                print(f"✅ NEU IMPORTIERT: '{filename}' (Auszahlung Nr. {payout_id}) mit {len(df)} Zeilen.")
                imported_payout_ids.add(payout_id)
                new_dfs.append(df)
                
        except Exception as e:
            # Andere CSV-Dateien leise ignorieren
            pass

    # 4. Zusammenführen und in Master-Datei speichern
    if new_dfs:
        all_new_data = pd.concat(new_dfs, ignore_index=True)
        
        if not existing_master_df.empty:
            final_master_df = pd.concat([existing_master_df, all_new_data], ignore_index=True)
        else:
            final_master_df = all_new_data
            
        final_master_df.to_csv(output_master_csv, sep=';', index=False, encoding='utf-8-sig')
        print(f"\n🎉 FERTIG: {len(new_dfs)} neue Datei(en) mit {len(all_new_data)} Zeilen in '{output_master_csv}' gespeichert.")
    else:
        print("\nℹ️ Keine neuen Auszahlungen gefunden. Master-Datei bleibt unverändert.")


if __name__ == "__main__":
    import_payout_files(input_directory=".", output_master_csv="Master_Payouts.csv")
