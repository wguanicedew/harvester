#!/usr/bin/env python3
"""Generate or refresh an IRI access token and store it in Panda as a user secret.

This script uses the vendored examples/get_globus_token.py to obtain tokens for
NERSC or ALCF. After obtaining a token it calls pandaclient.set_user_secret to
store the access token under the provided Panda secret key.

Two usage modes:
  Cron job (--refresh-only): non-interactive, intended to run unattended on a
    schedule. Only attempts to refresh a previously saved token; if the saved
    token cannot be refreshed, the script prints an error message and exits
    with a non-zero status instead of prompting for login.
  Interactive setup (--force-login): run manually to (re)generate the saved
    token. Forces an interactive Globus login flow so the user can open the
    consent URL and enter the authorization code.

Usage examples:
  python examples/generate_iri_token_to_panda.py --facilities nersc --refresh-only --panda_secret_key NERSC_IRI_ACCESS
  python examples/generate_iri_token_to_panda.py --facilities nersc --force-login --panda_secret_key NERSC_IRI_ACCESS

Exit codes: on refresh-only failure the script exits with non-zero status.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from get_globus_token import get_tokens, get_facility_token

try:
    # pandaclient is optional in examples; import when available
    import pandaclient
except Exception:  # pragma: no cover - example
    pandaclient = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate or refresh an IRI token and store it in Panda user secrets")
    parser.add_argument("--facilities", nargs="+", choices=["nersc", "alcf"], default=["nersc"], help="Facility to request token for")
    parser.add_argument("--token-file", type=Path, default=None, help="Path to saved Globus token JSON")
    parser.add_argument("--refresh-only", action="store_true", help="Cron job mode: only attempt refresh; exit non-zero with an error message if refresh fails, do not prompt for interactive login")
    parser.add_argument("--force-login", action="store_true", help="Interactive setup mode: force an interactive Globus login flow to (re)generate the saved token")
    parser.add_argument("--prompt-login", action="store_true", help="Add prompt=login to auth URL")
    parser.add_argument("--panda_secret_key", required=True, help="Panda secret key name to set the access token under")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        auth_data = get_tokens(args.facilities, token_file=args.token_file, refresh_only=args.refresh_only, force_login=args.force_login, prompt_login=args.prompt_login)
    except Exception as exc:
        print(f"Error obtaining token: {exc}")
        sys.exit(1)

    # pick the first facility token
    facility = args.facilities[0]
    try:
        token_data = get_facility_token(auth_data, facility)
    except Exception as exc:
        print(f"Failed to extract facility token: {exc}")
        sys.exit(2)

    access_token = token_data.get("access_token")
    if not access_token:
        print("No access_token found in token data")
        sys.exit(3)

    if pandaclient is None:
        print("pandaclient not available; cannot set Panda secret. Install pandaclient or run this in an environment with it.")
        sys.exit(4)

    # set the user secret
    try:
        pandaclient.set_user_secret(args.panda_secret_key, access_token)
        print(f"Set Panda user secret '{args.panda_secret_key}' successfully.")
    except Exception as exc:
        print(f"Failed to set Panda secret: {exc}")
        sys.exit(5)


if __name__ == "__main__":
    main()
