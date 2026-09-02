# Ersetze den Block "if uploaded_orders_db:" in der Sidebar durch diesen Code:

if uploaded_orders_db:
    try:
        df_ref = None
        
        if uploaded_orders_db.name.endswith(('.xlsx', '.xls')):
            xls = pd.ExcelFile(uploaded_orders_db)
            df_raw = pd.read_excel(uploaded_orders_db, sheet_name=xls.sheet_names[0], header=None)
            
            # Dynamische Suche nach der Kopfzeile (Sucht nach "Bestellnummer" oder "Order")
            header_idx = 0
            for idx, row in df_raw.iterrows():
                row_str = " ".join([str(val).lower() for val in row.values if pd.notna(val)])
                if "bestellnummer" in row_str or "order number" in row_str or "angebotstitel" in row_str:
                    header_idx = idx
                    break
            
            df_ref = pd.read_excel(uploaded_orders_db, sheet_name=xls.sheet_names[0], header=header_idx)
        else:
            content = uploaded_orders_db.getvalue().decode('utf-8', errors='ignore')
            lines = content.splitlines()
            header_idx = 0
            for i, line in enumerate(lines):
                line_lower = line.lower()
                if "bestellnummer" in line_lower or "order number" in line_lower or "angebotstitel" in line_lower:
                    header_idx = i
                    break
            df_ref = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), sep=None, engine='python')

        # Flexibles Spalten-Matching (deutsch & englisch)
        ref_order_col = next((c for c in df_ref.columns if any(x in str(c).lower() for x in ['bestellnummer', 'order number', 'order id', 'bestell-nr'])), None)
        ref_title_col = next((c for c in df_ref.columns if any(x in str(c).lower() for x in ['angebotstitel', 'artikelbezeichnung', 'item title', 'title', 'bezeichnung']) and 'nummer' not in str(c).lower() and 'id' not in str(c).lower()), None)
        ref_sku_col = next((c for c in df_ref.columns if any(x in str(c).lower() for x in ['bestandseinheit', 'custom label', 'sku'])), None)

        # Fallback: Falls 'Angebotstitel' fehlt, nach typischen eBay-Spaltenpositionen oder Alternativen suchen
        if not ref_title_col:
            ref_title_col = next((c for c in df_ref.columns if 'artikel' in str(c).lower() or 'item' in str(c).lower()), None)

        if ref_order_col and ref_title_col:
            df_ref['Match_Key'] = df_ref[ref_order_col].apply(clean_order_number)
            df_ref['Title_Val'] = df_ref[ref_title_col]
            df_ref['SKU_Val'] = df_ref[ref_sku_col] if ref_sku_col else ''
            
            added = save_orders_to_db(df_ref)
            st.sidebar.success(f"✅ {added} Artikelbezeichnungen erfolgreich importiert!")
        else:
            st.sidebar.error(f"❌ Spalten nicht gefunden. Erkannt wurden: {list(df_ref.columns[:5])}")
    except Exception as ex:
        st.sidebar.error(f"Fehler beim DB-Import: {ex}")
