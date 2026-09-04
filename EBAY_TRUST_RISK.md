# eBay Trust / Risk – Recovery

Stand: 04.09.2026. Ausschließlich Branch `codex/recover-payout-settlement`.

## Bedienung

Neuer Tab **Trust / Risk**, zusätzlich zu allen bestehenden Bereichen. Der Button
**eBay-Daten aktualisieren** startet einen ausdrücklich angeforderten Leseabruf.
Beim Öffnen, Tabwechsel oder normalen Streamlit-Rerun gibt es keine API-Aufrufe.

Zugangsdaten ausschließlich im lokalen Streamlit-Secrets-Abschnitt
`ebay_durchstart`: `client_id`, `client_secret`, `ru_name`, `refresh_token`.
Keine Umgebungsvariablen-Fallbacks oder Zugangsdaten in Git. Der Abschnitt ist in
der geprüften Recovery-Umgebung derzeit nicht verfügbar. Ohne ihn bleibt der
Abrufbutton deaktiviert. `ru_name` wird für die vorhandene Konfiguration verlangt;
im Refresh-Token-Flow wird es nicht an den Token-Endpunkt übertragen.

Die Tagesübersicht zeigt Account-Status, offene Rückgaben, Payment Disputes,
erkannte Holds, Fristen, regelbasierte Handlungsempfehlungen und Partnertexte.
**Text kopieren** erfolgt über das native Kopiersymbol im Streamlit-Textblock.
Es wird niemals eine Nachricht gesendet. Die Detailansicht enthält Fälle,
Service-Metriken, Datenverfügbarkeit und originale API-Felder.

## Architektur und Grenzen

- `ebay_readonly.py`: feste GET-Endpunktliste, OAuth-Refresh automatisch vor
  Ablauf und höchstens einmal nach HTTP 401. Einziger POST ist der OAuth-Refresh.
  Kein allgemeiner Schreibclient, keine Redirects, feste Timeouts. HTTP 403,
  OAuth-Fehler, 429, Netzwerkfehler und unvollständige Antworten werden getrennt
  sichtbar. Rate Limits setzen eine Wartefrist; keine automatische Retry-Schleife.
- `trust_risk.py`: vollständige paginierte Abrufe, separate versionierte
  API-Snapshots, Zuordnung und Auditregeln. API-Snapshots liegen ausschließlich in
  `PAYMENT_DATA_DIR/Ebay_Readonly/`, atomar gespeichert und vom Git ausgeschlossen.
  Keine Änderung an Bestell-, Payout-, Workflow- oder Lexware-Registern.
  Dieser neue Cache gehört nicht zum bisherigen Abrechnungs-ZIP-Export.
- `trust_risk_ui.py`: Darstellung mit bestehenden Streamlit-Komponenten und CSS.
- `test_trust_risk.py`: simulierte Antworten einschließlich OAuth, Seitengrenzen,
  Fehlerfällen, Betragsrekonstruktion, Zuordnung und UI ohne Netzwerkzugriff.

Abgerufen werden Seller Standards, aktuelle/projizierte INR-/INAD-Metriken für
EBAY_DE, offene Returns, offene/kürzlich geschlossene Payment Disputes, Payouts,
Seller Funds Summary und Transaktionen der letzten 90 Tage. Geschlossene Fälle
werden nicht als offen gezählt. Eine vorhandene eBay-Einstufung wird angezeigt,
es wird kein eigener Account-Score erfunden. Hoch/überdurchschnittlich bewertete
Service-Metriken und BELOW_STANDARD werden hervorgehoben.

Die KPI „Aktive Holds“ zählt erkannte FUNDS_ON_HOLD-Transaktionen im 90-Tage-Abruf,
nicht garantiert alle jemals einbehaltenen Positionen. Der kontoweite Hold-Betrag
wird separat aus Seller Funds Summary angezeigt. Fehlende Bereiche erscheinen
als nicht verfügbar; Summen kritischer Fälle sind dann ausdrücklich Untergrenzen.
Ein Datenstand älter als 24 Stunden wird als veraltet markiert. Ein fehlgeschlagener
neuer Abruf ersetzt den angezeigten Stand durch seine ehrliche Verfügbarkeitsmeldung;
vorherige Snapshots bleiben als separate Dateien erhalten.

Zuordnung ausschließlich über vorhandene Bestellnummern und – sofern die API
sie liefert – Artikel-/Transaktions-IDs. Partner stammen aus dem bereits vorhandenen
Bestellkatalog einschließlich MH-Normalisierung. Mehrere Partner in derselben
Bestellung ohne eindeutigen Artikelbezug bleiben **nicht zugeordnet**. Es entstehen
keine neuen Bestellungen. Wiederkehrende SKU-Fälle sind beobachtete Häufigkeiten,
keine Retourenquote und kein automatisch abgeleiteter Schuldnachweis.

Auditregeln: Frist heute/überfällig = kritisch; Frist morgen oder ACTION_NEEDED =
handeln; sonst beobachten. Fehlende Fristen werden nicht erfunden. Datumsanzeige
und Tagesgrenzen verwenden Europe/Berlin. Die „KI-Audit“-Zusammenfassung ist
ausdrücklich regelbasiert; kein externer LLM-Aufruf.

## Payout 7718008497 / 491,80 € – Prüfstand

**Live-Prüfung noch nicht möglich:** Der benötigte Streamlit-Secrets-Abschnitt
ist nicht verfügbar. Es wurde kein authentifizierter eBay-Aufruf ausgeführt.
Deshalb sind Transaktionsliste, positionsbezogene API-Holds und eine zuverlässige
Rekonstruktion der 491,80 € durch Live-API-Daten noch **nicht nachgewiesen**.
Der bereits vorhandene manuelle Abgleich wird dadurch weder ersetzt noch verändert.

Vorbereiteter Prüfweg im Tab:

1. Payout-Detail und sämtliche per payoutId gefilterten Transaktionen lesen.
2. Zusätzlich Transaktionen je vorhandener Bestellnummer dieses Payouts lesen,
   damit bestellbezogene Holds ohne Payout-ID sichtbar werden können.
3. IDs, transactionType, transactionStatus, bookingEntry, Betrag/Währung,
   orderId, payoutId, transactionMemo und zusätzliche API-Felder erhalten.
4. Nur eindeutig diesem Payout zugeordnete Bewegungen mit Status PAYOUT und
   CREDIT/DEBIT in EUR vorzeichenrichtig addieren. Holds, Processing und noch
   auszahlbare Mittel bleiben außerhalb dieser finalen Summe.
5. Unbekannte Status, fehlende Buchungsrichtung/Währung, unvollständige Seiten,
   widersprüchliche IDs oder ein abweichender API-Payoutbetrag verhindern eine
   positive Bestätigung. Differenz = eindeutig finale Summe minus 491,80 €.
6. Ergebnisse inklusive Transaktionen, Holds und Abdeckung als JSON-Prüfbericht
   herunterladen. Ein aktueller Hold-Status ist kein historischer Nachweis zum
   Auszahlungsdatum; ohne Payout-ID ist ein Hold nur bestellbezogen nachgewiesen.

Simulierter Nachweis im Test: 521,79 € CREDIT minus 29,99 € DEBIT = 491,80 €;
zusätzlicher FUNDS_ON_HOLD-Betrag wird nicht addiert. Das ist ein Regressionstest,
**kein Befund über die tatsächlichen API-Daten des Kontos**.

## Lokale Abschlussprüfung

- Vollständige Suite: **124 Tests, 116 erfolgreich, 8 übersprungen**. Die acht
  übersprungenen Tests benötigen hier nicht vorhandene Originaldatei-Fixtures.
  Die vorhandenen Masterdaten-Fixtures wurden separat aus dem bestehenden
  Recovery-Backup für die Tests bereitgestellt.
- 15 neue Tests einschließlich UI mit simuliertem Datenstand; Netzwerkzugriffe
  sind im UI-Test blockiert. OAuth-Refresh ist mit simulierten Antworten geprüft,
  nicht mit dem echten Konto.
- Recovery-App auf Port 8511 neu gestartet, Health-Endpunkt HTTP 200,
  neuer Tab und deaktivierter Abrufbutton bei fehlenden Secrets im Browser geprüft.
- Vorher-/Nachher-Fingerprints: Bestell-/Payoutdateien, Registerdateien und
  logischer SQLite-Inhalt unverändert. Keine Lexware-Aufrufe und keine echten
  eBay-Geschäfts-API-Aufrufe.

## Offizielle Referenzen

- [OAuth Refresh](https://developer.ebay.com/api-docs/static/oauth-refresh-token-request.html)
- [Finances-Transaktionen](https://developer.ebay.com/api-docs/sell/static/finances/transaction-info.html)
- [Payouts](https://developer.ebay.com/api-docs/sell/static/finances/payout-info.html)
- [Verkäuferstandards](https://developer.ebay.com/api-docs/sell/static/performance/seller-standards.html)
- [Payment Dispute Summaries](https://developer.ebay.com/api-docs/sell/fulfillment/types/api%3ADisputeSummaryResponse)
- [Returns-Suche](https://developer.ebay.com/devzone/post-order/post-order_v2_return_search__get.html)
- [Post-Order-Authentifizierung](https://developer.ebay.com/Devzone/post-order/concepts/MakingACall.html)

Die tatsächlich verfügbaren APIs hängen vom bestehenden Consent und den
Kontoberechtigungen ab. Es werden keine Scopes erweitert, Schlüssel provisioniert
oder andere eBay-Einstellungen geändert.
