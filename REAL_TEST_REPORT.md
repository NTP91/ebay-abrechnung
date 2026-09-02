# Realtest vom 02.09.2026

Branch: `codex/recover-payout-settlement`. Kein Merge nach main.
Alle Quelldateien nur lesend geöffnet; Masterdateien ausschließlich in temporären
Testverzeichnissen erzeugt. Keine Kundendaten/Originaldateien im Commit.

## Verwendete Originale

Vier Payout-CSVs für 7712804241, 7710027297 und zweimal 7700379513.
Bestellbericht vom 02.09.2026 (CSV) sowie `eBay evica 01.08-30.08 (1).xlsx`.
Die XLSX enthält einen Bestellbericht auf dem ersten Blatt und ein leeres
zweites Blatt (`Tabelle1`); sie ist kein Lexoffice-Rechnungsbeispiel.

## Tatsächliche Ergebnisse

| Kennzahl | Erwartet | Gemessen | Abweichung |
|---|---:|---:|---:|
| Hochgeladene Payout-Dateien | 4 | 4 | 0 |
| Eindeutige Payouts | 3 | 3 | 0 |
| Eindeutige Zeilen | 50 | 50 | 0 |
| Payout-Summe | 4.427,83 EUR | 4.427,83 EUR | 0,00 EUR |
| Bestellungen | 43 | 43 | 0 |
| Erstattungen | 6 | 6 | 0 |
| Sonstige Gebühren | 1 | 1 | 0 |
| Bestellbezogene Positionen mit Produktname | 49 | 49 | 0 |

| Auszahlung | Zeilen | Betrag abzüglich Kosten |
|---|---:|---:|
| 7700379513 | 2 | 279,90 EUR |
| 7710027297 | 14 | 584,67 EUR |
| 7712804241 | 34 | 3.563,26 EUR |

43 Bestellungen ergeben 4.895,17 EUR, sechs Erstattungen -465,56 EUR,
eine sonstige Gebühr -1,78 EUR. Summe: 4.427,83 EUR.
Alle 50 Zeilen tragen den Quellstatus `Betrag überwiesen`.
Alle 49 Produktnamen kommen vollständig aus den Payouts; keine offene Zuordnung.

| Partner | Zeilen inkl. Erstattungen | Saldo |
|---|---:|---:|
| 001 | 1 | -9,90 EUR |
| BA | 1 | 62,99 EUR |
| MK | 1 | 265,00 EUR |
| PP | 4 | 385,89 EUR |
| MH (alle Varianten) | 28 | 2.004,83 EUR |
| NB | 14 | 1.720,80 EUR |
| Ohne Partner: eBay-Gebühr | 1 | -1,78 EUR |

## Gefundene Ursachen und Reparaturen

1. Der CSV-Bestellbericht endet mit zwei regulären Abschlusszeilen mit anderer
   Spaltenzahl. Diese exakten Abschlussformen werden nun erkannt. Beliebige
   beschädigte Transaktionszeilen werden weiterhin nicht still übersprungen.
2. Payouts verwenden `Artikelnr.`; dieses Alias wird nun als Artikelnummer erkannt.
   Frühere Masterdateien mit zusätzlicher leerer Artikelnummer-Spalte werden
   verlustfrei zusammengeführt. Widersprüchliche Werte bleiben ein Fehler.
3. Der zuerst ergänzte Realtest nahm an, Erstattungen hätten Transaktionsnummern.
   Tatsächlich haben alle sechs keine. Die fünf vorhandenen Verkaufs-/Erstattungs-
   paare werden daher anhand der Bestellnummer getestet und bleiben getrennt.
   Keine Anpassung der Importdaten oder Beträge an Sollwerte.

## Dubletten und Wiederholbarkeit

- Die beiden Dateien zu 7700379513 enthalten identische eingelesene Datensätze;
  diese Auszahlung wird nur einmal verarbeitet.
- Wiederholter Upload aller vier Payout-Dateien fügt null Zeilen hinzu und
  verändert die gespeicherten Dateibytes nicht.
- Bestell-CSV: 30 Artikelpositionen; XLSX: 150 Artikelpositionen; gemeinsam 180.
  Sequenzieller Import ergänzt 30 auf 180. Wiederholungen ergeben null neue
  Positionen und unveränderte Dateibytes.
- Verkaufs- und Erstattungszeilen derselben Bestellung bleiben erhalten.

## Lexoffice-Positionsprüfung (offline)

Jeder der drei Payouts wird separat verarbeitet. Nur Gruppe-B-Bestellungen gehen
in dessen Rechnungs-Payload; Erstattungen bleiben in der Gutschriftenübersicht,
Gebühren werden weder Rechnung noch Kundengutschrift zugeordnet.
Geprüft gegen den historischen Rechenweg aus f803b06:

- jede Transaktion eine Position, quantity = 1;
- netAmount = round(Payout-Betrag / 1.19, 2), unverändert;
- taxRatePercentage = 19; discountPercentage = 0.5;
- name = vollständiger Payout-Produktname;
- description = SKU, Bestellnummer, Auszahlungsnummer in dieser Reihenfolge.

Keine Abweichungen dieser Felder gegenüber der historischen Formel.
Dies ist ausdrücklich KEIN Vergleich mit einer tatsächlich erzeugten Rechnung.
Sechs Erstattungen inklusive kleiner Teilbeträge werden separat mit dem echten
Payout-Betrag und spiegelbildlicher Provision ausgewiesen. Die genaue Kennzeichnung
vollständig/teilweise wird nicht aus dem Betrag geraten; das Payout benennt sie
einheitlich als Rückerstattung. Die Gebühr -1,78 EUR ist nicht enthalten.

## Testlauf

PowerShell:

```powershell
$env:EBAY_REAL_TEST_DIR='C:\Users\servi\Downloads'
python -B -m unittest test_recovery test_real_reports -q
```

22 Tests bestanden: 15 Regressionstests und 7 Realtests. Darunter Streamlit-
Interaktionen mit den echten, temporär gespeicherten Daten und Offline-Payloads
für alle drei Payouts. Ohne EBAY_REAL_TEST_DIR werden die Realtests explizit
übersprungen, nicht als bestanden ausgegeben. Es gab nur Streamlit-Hinweise zur
künftigen Ablösung von use_container_width; keine App-Ausnahmen.

## Ausstehender Prüfpunkt / keine Produktionsfreigabe

Das tatsächliche Lexoffice-Rechnungsbeispiel fehlt weiterhin. Daher kann die
vom Nutzer geforderte vollständige Realabnahme noch nicht als abgeschlossen
gelten. Gemäß der vorgegebenen Reihenfolge noch NICHT durchgeführt:

- Wiederanbindung und kontrollierter Test der produktiven Rechnungs-API;
- dauerhafte Rechnungssperren, Timeout-Wiederanlauf und Statusverwaltung;
- neue Gestaltung und zweite Ausbaustufe.

Es wurden keine API-Zugangsdaten gelesen/geändert und keine externen Rechnungen
oder Gutschriften erzeugt. Die CSV-Speicherung benötigt weiterhin einen
persistent angebundenen Datenträger; Cloud-Rebuild-Dauerhaftigkeit ist offen.
Nächster benötigter Input: Lexoffice-Rechnungsbeispiel (PDF oder Positionsdaten)
für den Preis-/Rabattvergleich. main bleibt unverändert.
