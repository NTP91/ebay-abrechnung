# Partner-Excel: Layoutprüfung vom 03.09.2026

Branch: `codex/recover-payout-settlement`, Ausgangsstand `6e82d0d`.
Nur die Partner-Einzelabrechnungen erhalten einen neuen Export. Gruppenübersichten,
Import, Partnerzuordnung, Status und Lexoffice-Payload/API bleiben unverändert.
Kein Merge, Push oder produktives Deployment.

## Inhalt und Quellen

- Zwei Blätter: Rechnung (Bestellungen), Gutschriften (alle vollständigen/teilweisen
  Erstattungen, mit dem ursprünglichen negativen Vorzeichen). Keine Gebühren.
- Acht fachliche Spalten, Menge immer 1. Titel, SKU und Bestelldatum stammen aus
  dem eindeutig zugeordneten Bestellbericht (`Verkauft am`).
- Ursprüngliches eBay-Brutto einschließlich Versand stammt aus
  `Transaktionsbetrag (inkl. Kosten)`, das Auszahlungsdatum aus `Auszahlungsdatum`.
  Fehlendes ursprüngliches Brutto wird nicht durch einen anderen Betrag ersetzt.
- Beide Blätter enthalten Partner, Gruppe, Payoutnummer(n), zugehörige
  Auszahlungsdaten, Provisionssatz, Positionshinweis und sechs Summen.
- Datum als Excel-Datum, Euroformat mit zwei Nachkommastellen, Textumbruch,
  abwechselnde Zeilenfarben, acht Spalten ohne technische Zusatzspalten,
  fixierte Kopfzeilen und Filter auf der Positionstabelle.
- Gruppe A behält den bisherigen Exportumfang über alle importierten Payouts;
  Gruppe B wird weiterhin pro ausgewähltem Payout heruntergeladen.

## Unveränderter Rechenweg

Der bisherige Nettobetrag `eBay_Netto = round(Erlös_Brutto / 1.19, 2)` bleibt als
verbindlicher Centbetrag erhalten. Partnerbrutto bleibt
`Erlös_Brutto * 0.995` (Gruppe A) bzw. `Erlös_Brutto * 0.965` (Gruppe B).
Die Anzeige mit zwei Nachkommastellen verändert die zugrunde liegenden Werte nicht.
Die ursprüngliche Abrechnungsbasis ist in der Bruttoformel enthalten; sie wird
nicht aus dem bereits gerundeten Netto zurückgerechnet oder durch das eventuell
abweichende ursprüngliche eBay-Brutto vor Kosten ersetzt.

Rabattbetrag = Netto vor Rabatt × Provisionssatz; Netto nach Rabatt = Netto minus
Rabatt; Umsatzsteuer = Netto nach Rabatt × 19 %. Bereits im bisherigen Rechenweg
entstehende Unterschiede gegenüber dem direkt berechneten Partnerbrutto werden
unter den Summen ausdrücklich als Rundungsdifferenz ausgewiesen, nicht durch eine
Änderung der Zahlungssumme ausgeglichen. Beispielsweise beträgt diese Differenz
bei den MH-Verkäufen des Testpayouts 0,008492 EUR (im Hinweis 0,0085 EUR).

## Originaldaten und Vergleich

Die sechs ursprünglichen Einzeldateien liegen nicht mehr am früher dokumentierten
Downloads-Pfad. Verwendet wurden ihre bereits importierten, vollständigen
Masterdaten aus der separaten lokalen Testinstanz: 50 Payout-Zeilen, 180
Bestellpositionen, 3 Payouts, Gesamtsaldo 4.427,83 EUR. Diese stimmen in Umfang und
Summen mit dem früheren Realtest überein. Für die UI-Prüfung wurden Kopien in einem
temporären Verzeichnis verwendet. SHA-256-Prüfung: beide Quelldateien unverändert.

Alle 49 Partnertransaktionen (43 Bestellungen, 6 Erstattungen) wurden über alle
Partnerdownloads geprüft, einschließlich BA, NB und zusammengefasstem MH.
Jeder Nettobetrag ist unverändert; jeder Bruttobetrag und jede Blattsumme stimmen
mit der bisherigen Partnerformel überein (Toleranz für binäre Fließkommadarstellung
1e-10 EUR; keine Centabweichung). Die einzelne Gebühr bleibt außerhalb der Exporte.

Testdownloads für Auszahlung **7712804241**:

| Partner | Blatt | Positionen | Netto vor Rabatt | Partnerbrutto vorher = nachher | eBay-Brutto inkl. Versand |
|---|---|---:|---:|---:|---:|
| BA | Rechnung | 1 | 52,93 | 62,67505 | 62,99 |
| BA | Gutschriften | 0 | 0,00 | 0,00 | 0,00 |
| NB | Rechnung | 7 | 1.114,53 | 1.279,8795 | 1.326,30 |
| NB | Gutschriften | 1 | -117,48 | -134,907 | -139,80 |
| MH | Rechnung | 19 | 1.412,48 | 1.622,0299 | 1.680,86 |
| MH | Gutschriften | 1 | -15,11 | -17,3507 | -17,98 |

Ungekürzte Bruttowerte dokumentieren die unveränderte Berechnung; in Excel werden
Geldbeträge mit zwei Nachkommastellen angezeigt. Keine Kundendaten oder fertigen
Originaldaten-Downloads werden eingecheckt.

## Validierung

```powershell
$env:EBAY_REAL_MASTER_DIR='<Testinstanz>\test-data'
$env:PARTNER_TEST_OUTPUT_DIR='<separates Ausgabeverzeichnis>'
python -B -m unittest test_partner_export test_recovery test_api_workflow -q
```

31 Tests bestanden (inklusive Original-Masterdaten und UI-Interaktionen für alle
drei Payouts). Zusätzlich unabhängiges Lesen aller erzeugten Excel-Dateien,
Formelfehlerprüfung und visuelle Prüfung beider Blätter der drei Testdownloads.
Geprüft: ursprüngliches Brutto abweichend vom Betrag nach Kosten, Menge unabhängig
von Bestellmenge, lange Titel/SKU, negative Teilbeträge, leere Blätter, alle
Partnergruppen und sprachunabhängige Datumsverarbeitung.

HTTP ist während der neuen Exporttests gesperrt. Bestehende API-Tests verwenden
ausschließlich Mocks. Keine echte Lexoffice-Rechnung oder Gutschrift erzeugt.

## Wartung

`templates/partner.xlsx` enthält ausschließlich das datenfreie Layout. Der
Entwicklungsbuilder `scripts/build_partner_template.mjs` nutzt artifact-tool.
Der App-Export füllt das OpenXML-Template mit der Python-Standardbibliothek;
keine neue Laufzeitabhängigkeit und kein Node-Prozess in der Streamlit-App.
