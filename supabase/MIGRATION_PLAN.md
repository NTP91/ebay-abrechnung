# Supabase-Schema-Migrationsplan — Durchstarter / ebay-abrechnung

Status: **Nur Schema (DDL). Keine Datenmigration, keine App-Umschaltung, kein Storage-Bucket.**

## Umfang dieser Migration

Die SQL-Dateien in `supabase/migrations/` legen ausschließlich Tabellen, Constraints,
Indizes und RLS-Sperren im Supabase-Projekt „Durchstarter" an. Es werden keine
Zeilen eingefügt, keine lokalen Dateien (CSV/SQLite/JSON) verändert oder gelöscht,
und die App liest/schreibt weiterhin ausschließlich lokal.

| Datei | Inhalt |
|---|---|
| `20260904090001_extensions.sql` | `pgcrypto` (UUID-Generierung) |
| `20260904090002_reference_tables.sql` | `partners`, `billing_recipients` |
| `20260904090003_orders_payouts.sql` | `payouts`, `orders`, `payout_transactions` (+ Parent/Child-Spalten, Trigger) |
| `20260904090004_audit_imports.sql` | `audit_log`, `imports`, `import_warnings` |
| `20260904090005_workflow_invoices.sql` | `position_workflow`, `discarded_invoices` (inkl. RE0089-Fähigkeit), `partner_invoices`, `partner_invoice_positions` |
| `20260904090006_holds_sync_reconciliation.sql` | `api_hold_evidence`, `ebay_sync_state`, `ebay_sync_runs`, `manual_payout_reconciliation`, `manual_payout_reconciliation_audit` |
| `20260904090007_trust_risk_snapshots.sql` | `trust_risk_snapshots` (+ View `trust_risk_latest`) |
| `20260904090008_rls_lockdown.sql` | Default-Deny: RLS überall aktiv, keine anon/authenticated-Policies, explizite REVOKEs |

## RLS-Modell

Jede Tabelle hat `ROW LEVEL SECURITY` aktiviert, aber **keine** Policy für `anon`
oder `authenticated`. In Postgres/Supabase bedeutet das: diese beiden Rollen sehen
und schreiben **null Zeilen**, unabhängig von Grants. `service_role` (der Schlüssel,
den ein künftiger GitHub-Actions-Service verwenden würde) umgeht RLS gemäß
Supabase-Konvention grundsätzlich und kann daher sicher schreiben, sobald die App
darauf umgestellt wird — ohne dass hier zusätzliche Policies nötig sind. Die
`REVOKE`-Anweisungen in Teil 8 sind zusätzliche Verteidigungstiefe, kein Ersatz für RLS.

## Partner_Invoices/ → Supabase Storage (nur Plan, noch nicht umgesetzt)

Heute: `partner_invoices.py` legt Originaldateien unter
`{PAYMENT_DATA_DIR}/Partner_Invoices/<sha256-hash><suffix>` ab (exklusiv erstellt,
max. 20 MB, `.pdf/.xlsx/.csv`), der Hash ist gleichzeitig Dedupe-Schlüssel und wird
in `partner_invoices.file_hash` (jetzt: Spalte in `public.partner_invoices`) gespiegelt.

Geplanter Übertragungsweg (später, nicht Teil dieses Auftrags):

1. Privaten Storage-Bucket anlegen (z. B. `partner-invoices`), **kein** öffentlicher Bucket.
2. Objektpfad-Konvention beibehalten: `<sha256-hash><suffix>` als Object-Key,
   identisch zum heutigen Dateinamen — Dedupe-Semantik bleibt 1:1 erhalten.
3. `partner_invoices.file_ref` (bereits als Spalte vorbereitet) erhält den
   Storage-Objektpfad statt des lokalen Dateinamens.
4. Zugriff ausschließlich über `service_role` bzw. signierte URLs mit kurzer
   Gültigkeit; Storage-RLS-Policies analog zum Tabellen-Default-Deny gestalten
   (kein anon/authenticated-Zugriff ohne explizite Policy).
5. Erst nach erfolgreicher Parallel-Validierung (Hash-Vergleich alter vs. neuer
   Speicherort) werden bestehende lokale Dateien hochgeladen — einmalig,
   idempotent, ohne die lokalen Originale zu löschen.

## Bewusst nicht umgesetzt

- Kein Storage-Bucket wurde angelegt (nur oben beschriebener Plan).
- Keine Zeilen wurden eingefügt (`partners`, `billing_recipients` etc. bleiben leer).
- Keine App-Datei (`core.py`, `app.py`, …) wurde geändert — die App liest/schreibt
  weiterhin ausschließlich lokale CSV/SQLite/JSON-Dateien.
- Keine Secrets wurden angelegt, gelesen oder in Supabase hinterlegt.
- Keine automatische Production-Deployment-Aktivierung (Supabase-GitHub-Integration
  bleibt manuell/Review-gesteuert; diese Migrationsdateien wurden nicht gegen das
  Live-Projekt ausgeführt).
- Die exakte Konsolidierungslogik für `orders` (Matching über Transaktionsnummer
  bzw. Bestellnummer+Artikelnummer, Konfliktprüfung) ist bewusst **nicht** als
  DB-Constraint nachgebaut — sie bleibt Anwendungslogik wie in `core.import_reports`
  heute, nur unterstützende Indizes wurden ergänzt.
- Der FK von `partner_invoice_positions.position_key` auf `position_workflow` ist
  `DEFERRABLE INITIALLY DEFERRED`, weil die exakte Schreibreihenfolge von
  `partner_invoices.approve()`/`position_workflow.confirm()` aus reinem Lesen des
  Codes nicht abschließend bestätigt werden konnte — vor dem Portieren des
  Schreibpfads erneut prüfen und ggf. verschärfen.
