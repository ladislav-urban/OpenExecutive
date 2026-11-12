#!/usr/bin/env python3
"""Print the seals OpenExecutive recorded for the ERPNext drafts it made.

oe_connector seals every draft's "OE Draft" row in a hash chain, and each
approval card here keeps its row's seal. The chain alone cannot show that
its newest rows were removed, or that someone with database access rewrote
it consistently; compared with these records, it can. On the ERPNext site:

    uv run --project packages/core python scripts/erpnext-draft-seals.py > seals.json   # set EPISODIC_DB_PATH if the API uses another ledger
    bench --site <site> oe-connector-verify-drafts --expect seals.json

In the Docker image, `openexecutive draft-seals` does the same
(`docker compose exec -T api openexecutive draft-seals > seals.json`).

The output is {card key: {"row_hash", "chain_index"}}, read from this
install's decision ledger (``integrations.erpnext_drafts.recorded_seals``).
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

CORE = pathlib.Path(__file__).resolve().parents[1] / "packages" / "core"
sys.path.insert(0, str(CORE))


def main() -> int:
    # The ledger's default path (./episodic_memory.db) is relative to where
    # the API runs; anywhere else it would read, or make, an empty one.
    if not os.environ.get("EPISODIC_DB_PATH"):
        os.chdir(CORE)
    from openexecutive.integrations.erpnext_drafts import recorded_seals

    seals = recorded_seals()
    json.dump(seals, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    print(f"{len(seals)} recorded seals", file=sys.stderr)
    # None means nothing to compare (or the wrong ledger): never a pass.
    return 0 if seals else 1


if __name__ == "__main__":
    sys.exit(main())
