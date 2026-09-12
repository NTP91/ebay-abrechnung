# Signierte eBay-Finances-Lesezugriffe

## Implementierung

`ebay_signature.py` erzeugt Ed25519-Signaturen nach der
[eBay-Vorgabe](https://www.developer.ebay.com/develop/guides/sell/digital-signatures-for-apis)
und RFC 9421. Die zentrale API-Schicht ergänzt bei jedem Finances-GET
`x-ebay-signature-key`, `Signature` und `Signature-Input`, auch auf Folgeseiten
und nach einer Token-Erneuerung. Andere APIs erhalten keine Signaturheader.

Signiert werden JWE, GET, URL-Pfad (ohne Query), Authority und die Signaturparameter
mit aktuellem UNIX-Zeitstempel. Für diese GET-Anfragen ohne Body ist kein
Content-Digest erforderlich. Fehlende, ungültige oder abgelaufene Schlüssel
verhindern den Aufruf; es gibt keinen unsignierten Fallback.

Schlüsselkonfiguration ausschließlich unter `st.secrets["ebay_durchstart"]`:
`signing_private_key`, `signing_jwe`, `signing_expiration`. Keine echten Werte in
Code, Git, Tests oder Protokollen. Der normale Client legt keine Schlüssel an.

Für die beauftragte erstmalige Einrichtung wurde ein ED25519-Schlüsselpaar regulär
über die eBay Key Management API erzeugt. Ein lokaler Setup-Marker verhindert
versehentliche Wiederholung; die nur einmal gelieferte Schlüsselantwort wurde
zusätzlich ausschließlich im ignorierten `.streamlit`-Verzeichnis gesichert.
Nach dieser Einrichtung waren alle fachlichen Testaufrufe GET-Anfragen.

## Live-Ergebnis 04.09.2026

Payout **7718008497**: **SUCCEEDED**, API-Betrag **491,80 EUR**, **26 von 26**
Transaktionen vollständig gelesen. Alle angefragten Datenquellen inklusive
Finances-Payouts, Transaktionen, bestellbezogenen Abfragen und Funds Summary
sind im signierten Abruf verfügbar.

- 14 SALE/CREDIT: **1.600,27 EUR**.
- 11 RETRO_HOLD-Abbuchungen: **−1.078,48 EUR**.
- 1 DISPUTE_HOLD-Abbuchung: **−29,99 EUR**.
- Saldo: **491,80 EUR**, Differenz: **0,00 EUR**.

Alle 26 Bewegungen tragen `transactionStatus=PAYOUT`, auch die Einbehalts-DEBITs.
Der Bankabgleich muss deshalb Typ, Vorzeichen und Bestellbezug berücksichtigen.
Die Auditansicht zeigt gebuchte Einbehaltsbewegungen zusätzlich zu Transaktionen
mit `FUNDS_ON_HOLD`; sie ändert keinerlei Partnerfreigaben.

Die einbehaltenen Bestellungen sind für diesen Payout eindeutig erkennbar.
`transactionMemo` wurde in den 26 Bewegungen nicht geliefert. Separate belastbare
Release-Termine/-Zusagen für die Retro-Einbehalte fehlen. Bei Mehrfachbestellungen
ist der Hold zunächst bestellbezogen und nicht allgemein einem einzelnen Artikel
zuteilbar.

Der vollständige lokale Bericht mit allen Transaktions-/Bestellnummern liegt im
Recovery-Datenverzeichnis unter `Ebay_Readonly/Signed_Payout_Check_20260904.md`,
die maschinenlesbare Auswertung daneben als JSON. Keine Kundendaten im Git-Bericht.

## Bewertung

Der Bankbetrag dieses Payouts ist automatisiert rekonstruierbar. Die manuelle
Abrechnungsfreigabe entfällt noch nicht. Dafür ist eine separat freigegebene,
getestete Verarbeitung von Gegenbuchungen, Teilbeträgen, späteren Releases und
payoutübergreifenden Bestellbewegungen erforderlich. `PAYOUT` allein darf keine
Freigabe erzeugen.

## Validierung

Vollständige Suite: **129 Tests, 121 erfolgreich, 8 wegen fehlender Originaldatei-
Fixtures übersprungen**. Neue Tests prüfen die Signatur kryptografisch, Manipulation,
Schlüsselablauf, fehlende Schlüssel, Hosts, Seitensignierung und den Fall eines
Einbehalts mit Status PAYOUT. Bestehende Signatur-/Tokenfehler geben keine Secrets aus.

Geschützte Quell-/Registerdateien und logischer SQLite-Inhalt stimmen nach dem
Live-Test mit dem vorherigen Fingerprint überein. Keine Änderungen an RE0089,
Lexware, Abschlüssen, Abrechnungsfreigaben oder `main`.
