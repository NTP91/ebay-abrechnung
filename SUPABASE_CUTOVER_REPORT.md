# Payment-Cutover – Prüfprotokoll 11.09.2026

## Ergebnis

- Operative Quelle: Supabase `payment_runtime_objects` / `payment_runtime_chunks`.
- Lokaler und Dropbox-Datenbestand: nur historische, unveränderte Sicherung.
- Fail-closed Start: `PAYMENT_BACKEND=supabase` sowie Projekt- und Zugriffsdaten sind Pflicht.
- Migriert und per SHA-256 zurückgelesen: Orders, Payouts, komplettes SQLite-Register,
  Hold- und Sync-Zustand, Partner-/Empfängerkonfiguration, Trust/Risk-Snapshot und NB0576-PDF.
- Fehlende optionale Payout-Reconciliation blieb als fehlendes Objekt erhalten; es wurde
  kein leerer oder erfundener Zustand angelegt.

## 1:1-Kontrolle

- Orders: 289 Zeilen, Quelldatei 50.176 Bytes.
- Payouts: 415 Zeilen, Quelldatei 228.629 Bytes.
- Register: 9 Payouts, 54 Workflowpositionen, 1 Partnerrechnung, 20 gesperrte
  Rechnungspositionen, 1 verworfener Testentwurf, 128 Auditereignisse.
- Originalbeleg NB0576: 78.317 Bytes; Dateihash und Registerhash identisch.
- eBay-Sync-Version: 13; Hold-Beobachtungen: 2.038.
- RE0089 bleibt verworfen; RE0090- und NB0576-Historie bleiben im unveränderten Register.

## Fachkontrollen

- `MAH-00422`: zwei Bewegungen, Partner FS, +1.150,00 EUR und -1.150,00 EUR.
- Normale `MH...`-Varianten bleiben Partner MH.
- Erstattung bleibt als eigene negative Bewegung erhalten.
- Keine Rechnung, Zahlung oder Abschlussaktion wurde im Cutover-Test ausgeführt.

## Laufzeittests

- Produktiver Supabase-Leselauf: erfolgreich.
- Isolierter Supabase-Namespace: Auditänderung nach neuem Öffnen des Registers erhalten.
- Streamlit: Healthcheck erfolgreich; vollständiger AppTest ohne Ausnahme.
- Dropbox-Kontrolle nach Migration und UI-Test: Hashes der sechs operativen Kerndateien unverändert.

## Betriebshinweis

Der aktuell bereitgestellte Management-Token besitzt Datenbank-Lese- und
Migrationsrechte, aber kein normales Datenbank-Schreibrecht. Deshalb nutzt der
Adapter für versionierte Schreibvorgänge den autorisierten Supabase-Migrationskanal.
Für den späteren Regelbetrieb ist ein eng begrenzter `service_role`-/Secret-Key
als Streamlit-Secret der sauberere Schreibkanal; das sollte separat kontrolliert
eingerichtet werden.
