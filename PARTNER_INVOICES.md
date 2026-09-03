# Eingehende Partnerrechnungen

In jeder Partnerkarte steht „Partnerrechnung hochladen und prüfen“ bereit. Partner auswählen, PDF/XLSX/CSV hochladen und den Prüfbericht ansehen. Die Originaldatei bleibt dauerhaft erhalten. Die Historie enthält zusätzlich einen Bereich „Eingangsrechnungen“.

## Abgleich und Freigabe

Der Sollbestand wird beim Upload aus den aktuell offenen, bereits ausgezahlten Positionen des ausgewählten Partners eingefroren. Partner und SKU stammen unverändert aus `load_master_data()`. Mengen, Netto-/Bruttobeträge und Rabatte werden mit `prepare_partner_export()` ermittelt. Es gibt keine zusätzliche Rabattberechnung.

Verglichen werden Bestellnummer, SKU, Artikel, Menge 1, vorhandene Netto-/Bruttobeträge, Rabatt und Gesamtbetrag. Fehlende oder zusätzliche Positionen, bereits verwendete Positionen und falsche Beträge werden konkret gemeldet. Geldbeträge haben höchstens 0,01 € Rundungstoleranz; der Rabattsatz muss exakt stimmen. Eine erkannte Summe der Bruttozeilen muss außerdem zur ausgewiesenen Gesamtsumme passen.

- **Grün:** Abgleich vollständig. „Geprüfte Rechnung freigeben“ speichert Prüfstatus und Belegzuordnung nach ausdrücklicher Bestätigung mit dem eingegebenen Namen.
- **Gelb:** Felder/Tabellen sind nicht sicher auslesbar. Eine manuelle Freigabe erfordert eine zusätzliche ausdrückliche Bestätigung und eine nachvollziehbare Begründung. Sie bleibt als `manual_override` sichtbar.
- **Rot:** Erkannte Abweichungen. Keine automatische Freigabe und kein manueller Override über diesen Ablauf.

Der bisherige beleglose Aufruf `confirm(..., action='review')` ist gesperrt. Freigabe, Positionsverknüpfungen und `reviewed_at` werden in derselben SQLite-Transaktion gespeichert. Veränderte Quellen, fehlende/veränderte Originaldateien, doppelte Freigaben und bereits verwendete Positionen blockieren. Die bestehenden Zahlungs- und Abschlussregeln bleiben bestehen. Bereits vorhandene historische Statusbestätigungen werden nicht rückwirkend verändert.

Der Name der freigebenden Person wird vom Nutzer angegeben; die lokale App verfügt nicht über eine Benutzeranmeldung. Dieser Name, Zeitpunkt, Freigabeart und gegebenenfalls Begründung werden im Audit gespeichert.

## Unterstützte Beleginhalte

CSV und XLSX benötigen erkennbare Spaltenüberschriften, beispielsweise:

`Bestellnummer;SKU;Artikel;Menge;Netto vor Rabatt;Rabatt %;Positionsbetrag brutto;Rechnungsnummer;Rechnungsdatum;Gesamtbetrag brutto`

Rechnungsnummer und -datum werden gespeichert, wenn erkennbar. Die Gesamtsumme kann als Metadatenzeile oder gleichbleibende Spalte enthalten sein. Bekannte Varianten und die bestehende Partner-Excel-Struktur werden ebenfalls gelesen. SKU im Zusatztext wird unterstützt. Der bestehende Excel-Export wird nicht verändert.

PDF wird lokal über Text- und Tabellenextraktion gelesen. Gut strukturierte Tabellen sind automatisch prüfbar. Scans, unlesbare Tabellen und unbekannte Layouts bleiben gelb; keine OCR und kein externer KI-Dienst. Es wird niemals stillschweigend angenommen, dass ein fehlendes Feld korrekt sei. Rechnung und Gutschriften werden getrennt geprüft. Größenlimit: 20 MB, höchstens 100 PDF-Seiten für die automatische Extraktion.

## Speicherung und Dublettenschutz

- `partner_invoices` enthält Metadaten, Dateihash, Originaldateiverweis, erkannten Inhalt, Sollbestand, Prüfbericht und Freigabeinformationen.
- `partner_invoice_positions` ordnet jede freigegebene Position höchstens einer Eingangsrechnung zu.
- Gleicher Dateihash wird als Doppelupload erkannt. Eine bereits verwendete Rechnungsnummer desselben Partners kann nicht erneut freigegeben werden. Die ursprüngliche Rechnung wird nicht überschrieben.
- Originaldateien liegen im Datenverzeichnis unter `Partner_Invoices/`, benannt nach SHA-256-Hash.
- `Settlement_Partner_Invoices.json` spiegelt Register und Positionsverknüpfungen unabhängig von SQLite. Vollständige Backups enthalten Register, Spiegel und Originaldateien.

Neue Imports nach einem Upload werden nicht nachträglich in dessen eingefrorenen Sollbestand aufgenommen. Sie bleiben für den nächsten Beleg offen. Die Partnerkarten zeigen weiterhin den kumulativen offenen Bestand.

## Lokale Prüfungen

Tests decken passende CSV-/XLSX-/PDF-Belege, bestehende Partner-Excel-Dateien, fehlende/zusätzliche Positionen, falsche Partner/SKU/Mengen/Beträge/Rabatte, unbekannte Scans, dokumentierte Overrides, Quelldatenänderungen, Dateimanipulation, Doppeluploads, doppelte Rechnungsnummern, konkurrierende Positionsfreigaben und Backup-Wiederherstellung ab. BA, NB und MH wurden zusätzlich mit aus Originaldaten erzeugten synthetischen Rechnungen ausschließlich auf einer isolierten Datenkopie geprüft. Keine echte Eingangsrechnung oder Lexware-Rechnung wurde dabei erzeugt oder freigegeben.
