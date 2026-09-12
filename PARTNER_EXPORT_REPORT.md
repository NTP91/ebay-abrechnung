# Partnerexport und Lexoffice-Eingabe: Prüfstand 03.09.2026

Repository `NTP91/ebay-abrechnung`, Branch `codex/recover-payout-settlement`.
Dieser Stand ersetzt die Exportberechnung aus `764ab2f` ausdrücklich: Das
Rechnungsbrutto wird jetzt aus den Lexoffice-Nettoeingaben berechnet. Ein
unabhängiger Rabatt auf den eBay-Kontrollbetrag entfällt.

## Abrechnungsarten und Stammdaten

| Abrechnungsart | Rabatt | Rechnungsempfänger |
|---|---:|---|
| Gruppe-A-Einzelabrechnung (PP, BA, MK, 001) | 0,5 % | Evelyn |
| Gruppe-B-Einzelabrechnung (NB, MH, weitere zugeordnete Partner) | 3,5 % | Patrick |
| Gesamtübersicht Gruppe B | 0,5 % | Evelyn |

Die Rate folgt ausschließlich der Abrechnungsart. Es gibt keine manuelle
Rabattsatzauswahl. Die bestehenden Gruppen und die MH-Zusammenfassung werden
aus dem unveränderten Master übernommen; NB bleibt separat.

`billing_recipients.json` enthält eine versionierte Stammdatenstruktur mit
Empfängername und getrennten Feldern für Namenszusatz, Straße, Postleitzahl,
Ort und Land. Die Adressfelder sind leer. Im Export steht entsprechend
„Rechnungsadresse noch nicht hinterlegt“. Eine externe Stammdatendatei kann
später über `PAYMENT_RECIPIENTS_PATH` eingebunden werden. Diese Datei definiert
keine Rabattsätze. Es wurden keine realen Adressen ergänzt oder erfunden.

## Einheitliche Ausgabe

Jede Datei enthält Rechnung und Gutschriften. Beide Blätter enthalten die
elf angeforderten Spalten in ihrer vorgegebenen Reihenfolge. Menge = 1,
Einheit = Stück. Artikelname stammt vollständig aus dem Bestellbericht;
Zusatztext enthält separat eBay-Bestellnummer und SKU.

Die Eingabespalten Artikelname bis Umsatzsteuer sind hellgrün, Bestelldatum,
Bestellnummer und die beiden Bruttokontrollen hellgrau. Negative Geldbeträge
sind rot. Kopfzeilen werden fixiert, Texte umbrochen und Geldwerte rechtsbündig
mit zwei Nachkommastellen dargestellt. Zahlen bleiben numerisch; Datumszellen
enthalten echte Excel-Daten. Vorschauen verwenden ausdrücklich deutsche
Dezimal- und Tausendertrennzeichen.

Im Header stehen Partner, Gruppe, Rabatt, Umsatzsteuer, Empfänger, Adressstatus,
alle eindeutigen Payoutnummern und der Auszahlungszeitraum. Der vorgeschriebene
Freitext-Hinweis steht rot über der Eingabehilfe. Der Begriff „Provision“ ist
in den vier sichtbaren Exporten nicht vorhanden.

## Berechnung und Rundung

Grundlage: [Lexware-Spaltenmethode](https://developers.lexware.io/cookbooks/bookkeeping/#berechnung-der-steuerbeträge)
und [Positionsrabatte](https://help.lexware.de/de-form/articles/547984-rabatte-in-ausgangsbelegen-hinterlegen).
Die [API-Dokumentation](https://developers.lexware.io/docs/#invoices-endpoint)
beschreibt Netto-Positionssummen mit zwei Nachkommastellen.

1. VK netto bleibt der bisherige Centwert aus `eBay_Netto`.
2. Je Position: `ROUND(VK netto × Menge × (1 − Rabatt), 2)`.
3. Die gerundeten Nettopositionen ergeben Netto nach Rabatt.
4. Umsatzsteuer: `ROUND(Netto nach Rabatt × 19 %, 2)`.
5. Rechnungsbrutto = Netto nach Rabatt + Umsatzsteuer.
6. Rabatt netto = Summe VK netto vor Rabatt − Netto nach Rabatt.
7. Rabatt brutto = eBay-Kontrollsumme − Rechnungsbrutto.

Kaufmännische Cent-Rundung wird in Python mit Decimal/ROUND_HALF_UP und in Excel
mit ROUND abgebildet, auch für negative Korrekturen. Die Steuer der gesamten
Nettorechnung wird als Differenz aufeinanderfolgender kumulierter Steuerbeträge
auf die Positionsbruttos verteilt. Damit summieren sich auch die sichtbaren
Positionsbruttos exakt zum Rechnungsbrutto. Diese Bruttospalte ist ein
Kontrollwert; in Lexoffice werden die grünen Nettoeingabefelder übernommen.
Der Rundungsweg wird unter den Summen kurz erklärt.

Einfache Hilfsformeln liegen in ausgeblendeten Berechnungszeilen unterhalb des
Druckbereichs, innerhalb derselben beiden Blätter. Es gibt keine zusätzlichen
technischen Spalten. Der eBay-Kontrollbetrag kommt in keiner Formel zur
Rechnungsberechnung vor.

NB-Regression: VK netto **329,83 EUR**, Rabatt **3,5 %**, Nettoposition **318,29 EUR**,
19 % Steuer **60,48 EUR**, Brutto **378,77 EUR**. Der frühere unabhängige Weg
`392,50 × 0,965` zeigte 378,76 EUR. Dieser Fehler ist behoben.

## Original-Testdaten und vier Testdateien

Verwendet wurden die vorhandenen Original-Masterdaten der isolierten Testinstanz:
50 Payoutzeilen, 180 Bestellpositionen, 3 Payouts, Gesamtsaldo 4.427,83 EUR.
Die Quellen wurden für UI-Tests in ein temporäres Verzeichnis kopiert.
SHA-256 vor/nach der Prüfung bestätigt unveränderte Quell-Masterdateien.

Die vier neuen Testdateien enthalten jeweils **alle** vorhandenen Transaktionen
des betreffenden Partners bzw. der Gruppe. Die Gruppe-B-Gesamtabrechnung ist
eine alternative Abrechnungsart und keine zusätzliche Partnerauszahlung.

| Datei | Payouts | Rechnung / Erstattung | Rechnungsbrutto | Gutschriftenbrutto |
|---|---|---:|---:|---:|
| BA | 7712804241 | 1 / 0 | 62,68 EUR | 0,00 EUR |
| NB | 7700379513, 7710027297, 7712804241 | 12 / 2 | 2.007,68 EUR | -347,11 EUR |
| MH | 7710027297, 7712804241 | 25 / 3 | 2.027,21 EUR | -92,58 EUR |
| Gruppe B an Evelyn | 7700379513, 7710027297, 7712804241 | 37 / 5 | 4.160,31 EUR | -453,38 EUR |

Je Excel-Datei ist eine PNG-Vorschau mit **beiden vollständigen Blättern** erzeugt.
Die Originaldaten-Ausgaben und Vorschauen bleiben außerhalb des Repositorys.

## Prüfung

- **32 Tests bestanden**: Export-, Originaldaten-, Import-/Matching-/Dubletten-
  Regressionen, UI-Interaktionen für alle drei Payouts und bestehende Mock-API-Tests.
- Alle erwarteten Positionen enthalten, keine doppelten Masterpositionen.
- Titel und Zusatztext gegen den zugeordneten Bestellbericht geprüft.
- Empfänger, Gruppen, automatische Rabattsätze und sämtliche Payoutnummern geprüft.
- Rechnung/Erstattungen getrennt, keine Gebühren in Partnerabrechnungen.
- Alle sieben Summen mit einer unabhängigen Rational-/Ganzzahlberechnung geprüft.
- **651 Formelzellen** nach einer erzwungenen Änderung/Rücksetzung der Eingabe
  unabhängig neu berechnet und mit den Cent-Sollwerten verglichen: keine Abweichung.
- Keine Formelfehler; alle acht Blätter visuell als deutsche PNG-Vorschau geprüft.

Reproduktion:

```powershell
$env:EBAY_REAL_MASTER_DIR='<Testinstanz>\test-data'
$env:PARTNER_TEST_OUTPUT_DIR='<separater Ausgabeordner>'
python -B -m unittest test_partner_export test_recovery test_api_workflow -q
```

Der Test erzeugt genau BA.xlsx, NB.xlsx, MH.xlsx und Gruppe_B_Evelyn.xlsx sowie
eine Prüfdatei. `scripts/preview_partner_exports.mjs` prüft die Formeln erneut und
erzeugt die vier PNGs im gebündelten artifact-tool/Sharp-Entwicklungsruntime.
Die Streamlit-App benötigt unverändert nur ihre bisherigen Python-Abhängigkeiten.

`core.py`, `importer.py` und die Abhängigkeiten sind unverändert. Der bestehende
Lexoffice-Payload/API-Weg, Import, Matching, Gruppenbildung, Dubletten und Status
wurden nicht angepasst. Keine reale Rechnung oder Entwurf erzeugt; kein Upload,
keine automatische Eingangsrechnungsprüfung implementiert. Der Betragsvergleich
erfolgte offline gegen den dokumentierten Eingabe-/Rundungsweg, ohne einen neuen
Beleg im Lexoffice-Konto anzulegen. Kein Merge nach main und kein Push/Deployment.
