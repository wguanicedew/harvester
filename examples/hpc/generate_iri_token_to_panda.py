#!/usr/bin/env python3
"""Generate or refresh an IRI access token and store it in Panda as a user secret.

This script uses the vendored examples/get_globus_token.py to obtain tokens for
NERSC or ALCF. After obtaining a token it calls pandaclient.Client.set_user_secret
to store the access token under the provided Panda secret key, and also writes an
iri_config.yaml at --iri_config (see --base_url and --resource_id).

Required packages:
  pip install panda-client globus-sdk

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
  python examples/generate_iri_token_to_panda.py --facilities nersc --refresh-only --validate-iri --panda_secret_key NERSC_IRI_ACCESS

--validate-iri makes a lightweight authenticated GET call to an IRI endpoint
(NERSC account/projects, or ALCF filesystem ls when --facilities alcf is used)
to confirm the obtained token is actually accepted before it gets stored as a
Panda secret. Override the endpoint with --iri-validate-url, or tune the ALCF
default with --alcf-validate-resource-id / --alcf-validate-path.

Exit codes: on refresh-only failure the script exits with non-zero status.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from get_globus_token import ALCF_HOME_RESOURCE_ID, get_facility_token, get_tokens, get_validate_url, validate_iri_token

try:
    # pandaclient is optional in examples; import when available
    from pandaclient import Client
except Exception:  # pragma: no cover - example
    Client = None


DEFAULT_BASE_URL = "https://api.iri.nersc.gov"
DEFAULT_RESOURCE_ID = "59e80c79-4dfd-4c53-9c07-7405685fcd37"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate or refresh an IRI token and store it in Panda user secrets")
    parser.add_argument("--facilities", nargs="+", choices=["nersc", "alcf"], default=["nersc"], help="Facility to request token for")
    parser.add_argument("--token-file", type=Path, default=None, help="Path to saved Globus token JSON")
    parser.add_argument("--refresh-only", action="store_true", help="Cron job mode: only attempt refresh; exit non-zero with an error message if refresh fails, do not prompt for interactive login")
    parser.add_argument("--force-login", action="store_true", help="Interactive setup mode: force an interactive Globus login flow to (re)generate the saved token")
    parser.add_argument("--prompt-login", action="store_true", help="Add prompt=login to auth URL")
    parser.add_argument("--panda_secret_key", required=True, help="Panda secret key name to set the access token under")
    parser.add_argument("--iri_config", type=Path, default=Path.cwd() / "iri_config.yaml", help="Path to also write iri_config.yaml")
    parser.add_argument("--base_url", default=DEFAULT_BASE_URL, help="IRI API base URL")
    parser.add_argument("--resource_id", default=DEFAULT_RESOURCE_ID, help="IRI resource ID")
    parser.add_argument("--validate-iri", action="store_true", help="Validate the obtained token against an IRI endpoint before storing it")
    parser.add_argument("--iri-validate-url", default=None, help="Explicit IRI GET endpoint used by --validate-iri. Defaults to NERSC account/projects for nersc and ALCF filesystem ls for alcf.")
    parser.add_argument("--alcf-validate-resource-id", default=ALCF_HOME_RESOURCE_ID, help=f"ALCF resource_id used for the default --validate-iri filesystem ls (default: {ALCF_HOME_RESOURCE_ID}, Home)")
    parser.add_argument("--alcf-validate-path", default=None, help="ALCF filesystem path used for the default --validate-iri filesystem ls (default: /home/$USER/)")
    return parser.parse_args()


IRI_CONFIG_TEMPLATE = """# IRI API client configuration
# Copy this file to e.g. ~/.iri.yaml and fill in your values.

base_url: {base_url}

resource_id: {resource_id}
access_token: {token}
"""


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

    if args.validate_iri:
        try:
            validate_url = get_validate_url(facility, args.iri_validate_url, args.alcf_validate_resource_id, args.alcf_validate_path)
            validate_iri_token(token_data, validate_url)
        except RuntimeError as exc:
            print(f"IRI token validation failed: {exc}")
            sys.exit(7)
        print(f"Validated token against the IRI API successfully ({validate_url}).")

    if Client is None:
        print("pandaclient not available; cannot set Panda secret. Install pandaclient or run this in an environment with it.")
        sys.exit(4)

    # set the user secret
    status, (success, message) = Client.set_user_secret(args.panda_secret_key, access_token)
    if status != 0 or not success:
        print(f"Failed to set Panda secret: status={status} message={message}")
        sys.exit(5)
    print(f"Set Panda user secret '{args.panda_secret_key}' successfully.")

    content = IRI_CONFIG_TEMPLATE.format(token=access_token, base_url=args.base_url, resource_id=args.resource_id)
    try:
        args.iri_config.parent.mkdir(parents=True, exist_ok=True)
        args.iri_config.write_text(content, encoding="utf-8")
        print(f"Wrote IRI config to {args.iri_config}")
    except Exception as exc:
        print(f"Failed to write IRI config: {exc}")
        sys.exit(6)


if __name__ == "__main__":
    main()
