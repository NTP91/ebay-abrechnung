"""Daily production payout sync: eBay Finance API -> Supabase.

Runs the existing, unmodified ebay_sync.run() -- no changes to merge, dedup,
validation, billing, MH, Lexware or UI logic. This only wires that existing
entrypoint to a GitHub Actions runner instead of a human/Codex-dependent
machine.

Trigger mapping: GitHub's schedule event -> ebay_sync trigger 'automatic';
workflow_dispatch (manual test run) -> trigger 'manual', matching the existing
UI button semantics.

DST/late-start guard: the workflow's cron fires twice a day (one CET offset,
one CEST offset), so 21:59 Europe/Berlin is always covered without seasonal
edits, and a late GitHub start is tolerated. Before touching the eBay API,
scheduled runs check whether an 'automatic' run already succeeded today
(Europe/Berlin calendar date, read from the existing ebay_sync state in
Supabase) and exit cleanly if so -- at most one real import happens per day,
regardless of which of the two triggers fires first.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BERLIN = ZoneInfo('Europe/Berlin')


def ebay_provider():
    """Build the ebay_readonly secrets_config() shape from GitHub Actions secrets."""
    from ebay_readonly import EbayError
    values = {
        'client_id': os.environ.get('EBAY_CLIENT_ID', ''),
        'client_secret': os.environ.get('EBAY_CLIENT_SECRET', ''),
        'ru_name': os.environ.get('EBAY_RU_NAME', ''),
        'refresh_token': os.environ.get('EBAY_REFRESH_TOKEN', ''),
    }
    if not all(values.values()):
        raise EbayError('Zugangsdaten nicht verfuegbar: EBAY_CLIENT_ID/EBAY_CLIENT_SECRET/EBAY_RU_NAME/EBAY_REFRESH_TOKEN erforderlich.')
    for key, env_name in (('signing_private_key', 'EBAY_SIGNING_PRIVATE_KEY'),
                           ('signing_jwe', 'EBAY_SIGNING_JWE'),
                           ('signing_expiration', 'EBAY_SIGNING_EXPIRATION')):
        value = os.environ.get(env_name, '')
        if value:
            values[key] = value
    return values


def already_ran_today(berlin_today):
    import supabase_store
    doc, _ = supabase_store.get_json('state/ebay_sync.json', default=None)
    if not doc:
        return False
    for run in doc.get('runs', []):
        if run.get('trigger') != 'automatic' or run.get('status') != 'success':
            continue
        try:
            when = datetime.fromisoformat(str(run.get('at', '')).replace('Z', '+00:00'))
        except ValueError:
            continue
        if when.astimezone(BERLIN).date() == berlin_today:
            return True
    return False


def main():
    os.environ.setdefault('PAYMENT_BACKEND', 'supabase')
    event = os.environ.get('GITHUB_EVENT_NAME', 'workflow_dispatch')
    trigger = 'automatic' if event == 'schedule' else 'manual'
    print(f'Ausloeser: {event} -> ebay_sync trigger={trigger}')

    if trigger == 'automatic':
        berlin_today = datetime.now(BERLIN).date()
        if already_ran_today(berlin_today):
            print(f'Heutiger automatischer Payout-Sync ({berlin_today}, Europe/Berlin) bereits erfolgreich gelaufen. Kein erneuter Import.')
            return 0

    import core
    from ebay_readonly import Client
    import ebay_sync

    directory = Path(core.PAYOUTS_DB_PATH).resolve().parent
    client = Client(provider=ebay_provider)
    result = ebay_sync.run(directory, trigger, client)

    print(json.dumps(result, indent=2, ensure_ascii=False))

    if result.get('status') != 'success':
        print(f"::error::Payout-Sync nicht erfolgreich (status={result.get('status')}): {result.get('error') or 'siehe Log'}")
        return 1

    import round_planner
    assigned = round_planner.assign_late_payouts()
    if assigned:
        print(f"Verspaetete Positionen nachtraeglich zugeordnet: "
              f"{json.dumps(assigned, ensure_ascii=False)}")
    else:
        print('Keine verspaetete Position faellig zur Nachzuordnung - No-Op.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
