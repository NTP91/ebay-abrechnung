# TAB GRUPPE B
        with tab_b:
            st.info("""
            **Gruppe B (Über Dich / Evelyn Kukulan inkl. Partner NB):**
            - **An Evelyn senden:** Rechnungsentwurf direkt per Button an **Lexoffice (Kundennummer 16335)** übermitteln (mit 0,5 % Rabatt).
            - **Partner-Downloads:** Unten findest du für jedes Partner-Kürzel die Aufschlüsselung inkl. **3,5 % Rabatt**, damit dir die Partner ihre Rechnung stellen können.
            """)
            st.write(f"**Anzahl Gesamt:** {len(df_grp_b)} Positionen | **Gesamtsumme Netto:** {df_grp_b['eBay_Netto'].sum():.2f} €")
            st.dataframe(df_grp_b[['Bestellnummer', 'SKU', 'eBay_Netto', 'Datum der Transaktionserstellung']], use_container_width=True)

            # --- NEU: Schnelle & übersichtliche Partner-Downloads direkt darunter ---
            st.markdown("### 📥 Partner-Downloads & Aufschlüsselung (3,5 % Rabatt)")
            
            partner_prefixes_b = df_grp_b['SKU_Prefix'].unique()
            if len(partner_prefixes_b) > 0:
                for prefix in sorted(partner_prefixes_b):
                    df_partner_b = df_grp_b[df_grp_b['SKU_Prefix'] == prefix].copy()
                    
                    # 3,5% Rabatt berechnen (Netto * 0.965)
                    df_partner_b['Netto_abzgl_3_5_Prozent'] = (df_partner_b['eBay_Netto'] * 0.965).round(2)
                    
                    summe_netto = df_partner_b['eBay_Netto'].sum()
                    summe_rabatt = df_partner_b['Netto_abzgl_3_5_Prozent'].sum()
                    anzahl = len(df_partner_b)
                    
                    # Übersichtlicher Expander pro Partner
                    with st.expander(f"📦 **Partner/Kürzel: {prefix}** — ({anzahl} Positionen | Netto: {summe_netto:.2f} € | **Auszahlung -3,5%: {summe_rabatt:.2f} €**)"):
                        export_cols_b = ['Bestellnummer', 'SKU', 'eBay_Netto', 'Netto_abzgl_3_5_Prozent', 'Datum der Transaktionserstellung']
                        
                        # Download-Button prominent oben drüber
                        csv_data_b = df_partner_b[export_cols_b].to_csv(index=False, sep=';').encode('utf-8')
                        st.download_button(
                            label=f"📥 CSV Herunterladen für {prefix} (inkl. 3,5 % Rabatt)",
                            data=csv_data_b,
                            file_name=f"Abrechnung_Partner_{prefix}.csv",
                            mime="text/csv",
                            key=f"dl_b_{prefix}"
                        )
                        
                        st.dataframe(df_partner_b[export_cols_b], use_container_width=True)
            else:
                st.write("Keine Partner-Positionen in Gruppe B vorhanden.")
