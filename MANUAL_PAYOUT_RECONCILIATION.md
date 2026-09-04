# Manueller positionsbezogener Payout-Abgleich

In der Recovery-App steht oberhalb der Haupttabs der aufklappbare Bereich **Payout-Abgleich · Bankbetrag und einzelne Positionen**. Payout wählen, tatsächlichen Bankbetrag eintragen und jede finanzielle Quellbewegung als `freigegeben`, `einbehalten` oder `unklar` markieren. Alle 15 Finanzbewegungen des Referenzfalls werden angezeigt; die zwei Child-Artikelreferenzen stehen separat darunter und werden nicht erneut summiert.

`freigegeben` bedeutet im Bankabgleich **berücksichtigte Bewegung**. Dazu kann eine negative Belastung gehören. Der Rücksendungseinbehalt von -29,99 EUR wird deshalb berücksichtigt, aber durch die unveränderte Einbehalts-/Partnerlogik nicht zur Partner-Rechnungsposition.

Der Status `abgestimmt` wird nur bei exakt null Differenz, vollständiger Zuordnung aller Finanzbewegungen und unveränderten Quellen berechnet. Differenz = berücksichtigte Bewegungen minus Bankbetrag. Einbehaltene und unklare Positionen sind nicht abrechnungsfähig. Solange ein aktivierter manueller Abgleich nicht vollständig passt, bleiben auch seine freigegebenen Positionen gesperrt. Bestehende Zuordnungsprobleme werden durch einen passenden Bankbetrag nicht aufgehoben.

Ohne gespeicherten manuellen Abgleich bleibt der bisherige Ablauf für den betreffenden Payout bestehen. Die anderen vier Payouts wurden nicht automatisch als final ausgezahlt bewertet. Für Payouts mit Lexware-Reservierung/Übertragung oder bereits bestätigter Prüfung/Zahlung/Abschluss ist dieser Abgleich vorsichtshalber nur lesbar; keine vorhandene Bestätigung wird geöffnet oder überschrieben.

Eine spätere Änderung von `einbehalten` zu `freigegeben` aktualisiert die vorhandene Abgleichsentscheidung mit derselben Identität. Keine Bestellung oder Quelltransaktion wird neu angelegt. Erneute Abrechnungsfähigkeit setzt einen wieder passenden, belegten Bankabgleich voraus. Diese Funktion bucht keine Position automatisch in einen anderen Payout um und erfindet keinen späteren Bankeingang.

## Speicherung und Schutz

Bankbetrag, Entscheidungen, Quellprüfsummen, Name, Belegbegründung und vollständige vorherige/nachfolgende Entscheidungen werden getrennt von den Quelldaten gespeichert. Ablage: SQLite-Tabelle `manual_payout_reconciliation` und atomar geschriebene, versionierte `Settlement_Payout_Reconciliation.json`. Beim Lesen wird die neueste konsistente Kopie verwendet; gleich versionierte Widersprüche sperren. Backups enthalten beide Ablagen. Veraltete Ansichten, geänderte Quellen, neue Positionen und unklare Identitäten verhindern eine unbemerkte Freigabe. Nutzername ist eine dokumentierte Angabe, keine verifizierte Anmeldung.

Keine Änderung an Partnerzuordnung, Rabattberechnung, Exportlayout, Dublettenimport, Lexware-Aufrufen oder bestehenden Abschlussbedingungen. Die zusätzliche Abrechnungsprüfung wird vor Übergabe der Positionen an diese bestehenden Abläufe angewandt.

## Durchgeführter Abgleich am 04.09.2026

Payout **7718008497**, anhand eBay-Screenshots 09:31:45, 09:32:10 und 09:32:19:

- Bankbetrag: **491,80 EUR**.
- Positive Bestellbewegungen: 1.600,27 EUR.
- Davon 11 Bestellungen manuell einbehalten: 1.078,48 EUR.
- Drei freigegebene Bestellungen: 521,79 EUR.
- Zusätzlich berücksichtigter Rücksendungseinbehalt: -29,99 EUR.
- Berücksichtigte Bewegungen: **491,80 EUR**, Differenz **0,00 EUR**, Status **abgestimmt**.

Freigegebene Bestellungen: `12-15109-90247` (86,99 EUR), `17-15045-32069` (119,80 EUR), `23-15033-58445` (315,00 EUR). Der Rücksendungseinbehalt gehört zu `06-15117-56051`, Referenz `5328339477`.

Einbehaltene Bestellungen:

| Bestellung | Partner | Betrag EUR | Änderung |
|---|---|---:|---|
| 04-15124-90902 | MH | 97,98 | nicht mehr abrechnungsfähig |
| 20-15095-42050 | MH | 40,99 | nicht mehr abrechnungsfähig |
| 18-15098-98824 | MH | 168,98 | nicht mehr abrechnungsfähig |
| 16-15102-50100 | MH | 86,99 | nicht mehr abrechnungsfähig |
| 05-15122-99526 | MH | 26,90 | nicht mehr abrechnungsfähig |
| 20-15095-08458 | MH | 86,99 | nicht mehr abrechnungsfähig |
| 02-15128-14632 | BA | 62,99 | nicht mehr abrechnungsfähig |
| 25-15085-99747 | NB | 289,80 | nicht mehr abrechnungsfähig |
| 19-15096-64665 | PP | 27,00 | nicht mehr abrechnungsfähig |
| 21-15092-69082 | FS | 39,90 | schon zuvor wegen Partnerzuordnung gesperrt |
| 18-15098-91741 | Mehrfachbestellung | 149,96 | schon zuvor wegen Parent-Zuordnung gesperrt |

## Auswirkungen auf offene Partnerrechnungen

Mit der unveränderten Exportberechnung, inklusive deren Rundung:

| Partner | Positionen vorher → nachher | Bruttorechnung vorher EUR | nachher EUR |
|---|---|---:|---:|
| 001 | 2 → 2 | 432,64 | 432,64 |
| BA | 2 → 1 | 125,35 | 62,68 |
| MK | 1 → 1 | 263,68 | 263,68 |
| PP | 10 → 9 | 1.024,65 | 997,78 |
| MH | 44 → 38 | 3.506,26 | 3.015,23 |
| NB | 22 → 21 | 4.511,11 | 4.231,45 |

Gruppe A: Summe der offenen Partner-Bruttorechnungen von 1.846,32 auf 1.756,78 EUR (-89,54 EUR). Gruppe B: Summe der Einzelrechnungen mit 3,5 % von 8.017,37 auf 7.246,68 EUR (-770,69 EUR); dies ist nicht die Patrick-an-Evelyn-Gesamtrechnung mit 0,5 %.

Alle ursprünglichen CSVs, Positionsidentitäten, Prüf-/Zahlungs-/Abschlussmarkierungen, Eingangsrechnungen und die drei RE0089-Payoutreservierungen wurden nach dem Abgleich auf unveränderten Inhalt geprüft. Keine HTTP-/Lexware-Aufrufe beim Abgleich (technisch blockiert).

Sicherung und maschinenlesbarer Bericht außerhalb des Repositorys: `%LOCALAPPDATA%/PaymentTool-Test/Before-Manual-Payout-Reconciliation-20260904.zip` und `Manual-Payout-Reconciliation-20260904.json`.

## Tests

Gesamtsuite: 109 Tests, davon 101 bestanden und 8 mangels zusätzlicher Originaldatei-Fixtures übersprungen. Elf neue Tests prüfen exakte Abstimmung, Einbehalt, Differenz, unklare Positionen, spätere Freigabe ohne Doppelanlage, RE0089-/Abschluss-Schutz, konkurrierende Änderungen, veränderte und neue Quellen, redundante Speicherung/Backup, Workflow-Sperre, UI und den Referenzfall 491,80 EUR einschließlich Parent-/Child-Zeilen. Zusätzlich wurde die Recovery-Datenkopie mit dem tatsächlichen Abgleich im Streamlit-AppTest ohne UI-Ausnahmen geprüft.
