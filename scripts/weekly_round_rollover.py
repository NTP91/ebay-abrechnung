"""Automatic weekly round rollover (2026-003+): create the neutral round
shell for whichever week is current, plus any earlier shell a missed
scheduler run left out, using the existing round_planner.rollover().

Wires that existing, unmodified planning/creation logic to a GitHub Actions
runner instead of a human/Codex-dependent machine. No position is ever moved
between rounds here; a round is only ever created once (rollover() is a true
no-op for every already-existing sequence), and no round is finalized here.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    os.environ.setdefault('PAYMENT_BACKEND', 'supabase')
    import round_planner

    results = round_planner.rollover()
    created = [rid for rid, was_created in results if was_created]
    print(json.dumps({'checked': [rid for rid, _ in results], 'created': created}, ensure_ascii=False))
    if created:
        print(f"Neu angelegt: {', '.join(created)}")
    else:
        print('Keine neue Runde faellig - No-Op.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
