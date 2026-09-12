# Parent-/Child-Payout-Import · 04.09.2026

Artikelzeilen mit Transaktions- und Artikelnummer, aber ohne finanziellen Betrag, bleiben als Quellzeilen erhalten. Sie werden ausschließlich bei einer eindeutigen Parent-Zeile derselben Bestellung und desselben Payouts akzeptiert. Die vollständige Gruppe muss mindestens zwei Child-Zeilen enthalten. Artikel-Zwischensummen inklusive ausgewiesenem Versand müssen exakt dem Bruttogesamtbetrag der Kopfzeile entsprechen. Der Betrag nach Kosten stammt ausschließlich aus der Kopfzeile. Fehlende Beträge werden nicht zu null umgewandelt.

Die Prüfung erfolgt vor Speicherung auf dem zusammengeführten Payout-Bestand, sodass ein überlappender Teilbericht bereits bekannte Child-Zeilen enthalten darf. Unbekannte zusätzliche Positionen in gesperrten Payouts bleiben gesperrt. Child-Referenzen bleiben für die bestehende Transaktions-/Bestell-/SKU-Zuordnung verfügbar, werden aber nicht als eigene Finanzpositionen ausgegeben. Es gibt keine Aufteilung des Parent-Betrags auf Partner oder Artikel. Eine mehrdeutige Parent-Bestellzuordnung bleibt daher gemäß bisheriger Logik prüfpflichtig.

## Tatsächlicher Recovery-Import

Datei: `Payout_7718008497_20260904.csv` aus dem Download-Verzeichnis.

- 17 Quellzeilen übernommen: 13 vorhandene offene Zeilen aktualisiert, 4 neue Zeilen.
- 15 finanzielle Quellzeilen, zusammen 1.570,28 EUR; 2 nichtfinanzielle Child-Referenzen.
- Bestellung `18-15098-91741`: 99,98 + 49,98 = 149,96 EUR; Parent-Betrag genau einmal berücksichtigt.
- Child-Transaktionen `10087571312118` und `10087571312218` bleiben eindeutig zum Bestellbericht zuordenbar.
- Wiederholungsimport: 17 bekannt, 0 hinzugefügt; Payout-CSV bytegleich.
- Gesamtbestand: 5 Payouts.
- Zwei Zuordnungsprüfungen im neuen Payout: mehrdeutige Parent-Bestellung sowie unbekannter Partner FS bei Bestellung `21-15092-69082`. Keine automatische Abrechnungsfreigabe.
- Bestellbestand, Zahlungs-/Prüfstatus, Eingangsrechnungsregister und bestehende Lexware-Sperren unverändert. Keine HTTP-/Lexware-Aufrufe beim Testimport (technisch blockiert).

Vorherige Sicherung und maschinenlesbares Importprotokoll liegen unter `%LOCALAPPDATA%/PaymentTool-Test/`: `Before-Parent-Child-20260904.zip`, `Parent-Child-Import-20260904.json`.

## Tests

98 Tests ausgeführt: 90 bestanden, 8 wegen nicht vorhandener separater Originaldatei-Fixtures übersprungen. Neue Tests prüfen Parent plus zwei Child-Zeilen, fehlende Beträge ohne Nullbuchung, einmalige Gesamtsumme, erhaltene Transaktions-/SKU-Zuordnung, Summenabweichungen, fehlende Parent-Zeilen, fehlerhafte Einzelbestellungen, vollständigen und partiellen Re-Import, veränderte Child-Beträge und bestehende Payout-Sperren. Zusätzlich AppTest auf isolierter Kopie mit dem realen heutigen Import: keine UI-Ausnahme.
