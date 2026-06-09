# competitor-monitor

Weekly competitor content-gap monitor for murf.ai. See CLAUDE.md for the
full build spec, pipeline contract, and milestone order.

Quick start (after build):
1. Apply schema.sql in the Supabase SQL editor.
2. cp .env.example .env and fill in.
3. python scripts/refresh_inventory.py        # build Murf inventory
4. python main.py --dry-run                   # baseline run, no alerts
