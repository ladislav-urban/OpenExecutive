#!/usr/bin/env python3
"""Make the key pair for signed sign-ins (docs/auth.md, "Signed callers").

The web app signs who is signed in on every request it passes to the API, so
the API no longer takes that on trust from whoever holds
BACKEND_SHARED_SECRET. This prints the two settings:

    uv run --with cryptography python scripts/make-caller-keys.py

- CALLER_ASSERTION_PRIVATE_KEY goes on the web app (packages/ui) only. It
  signs; keep it as secret as BACKEND_SHARED_SECRET, and never give it to the
  API.
- CALLER_ASSERTION_PUBLIC_KEYS goes on the API only. It can check a signature
  but not make one.

Set both and restart both apps. To rotate: make a new pair, add its public
key to the API's list (comma-separated) and restart the API, move the web app
to the new private key, then drop the old public key.

With --erpnext it makes the separate pair for ERPNext phase 2
(docs/deployment.md, "ERPNext (oe_connector)"): ERPNEXT_CALLER_PRIVATE_KEY on
the API, which signs who is asking into each ERPNext call, and the public key
for oe_connector's site config, which checks it. Never reuse the web app's pair.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "packages" / "core"))

from openexecutive.api.caller_signing import KID_RE, make_key_pair  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--kid",
        help="a name for this key: letters, digits, _ or -, up to 32 (default: random)",
    )
    parser.add_argument(
        "--erpnext",
        action="store_true",
        help="make the pair for ERPNext phase 2 (the API and oe_connector) instead",
    )
    args = parser.parse_args()
    if args.kid is not None and not KID_RE.fullmatch(args.kid):
        print("--kid must be 1-32 letters, digits, _ or -", file=sys.stderr)
        return 2
    private, public = make_key_pair(args.kid)
    if args.erpnext:
        print("# The API only (packages/core). Secret:")
        print(f"ERPNEXT_CALLER_PRIVATE_KEY={private}")
        print("# ERPNext, in the bench:")
        print(f"bench --site <site> set-config oe_connector_caller_public_keys '{public}'")
        return 0
    print("# The web app (packages/ui) only. Secret:")
    print(f"CALLER_ASSERTION_PRIVATE_KEY={private}")
    print("# The API only:")
    print(f"CALLER_ASSERTION_PUBLIC_KEYS={public}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
