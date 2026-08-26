#!/usr/bin/env python3
"""Generate a Globus HTTPS access token for a single HPC site and store it in Panda
as a user secret.

Unlike generate_transfer_token_to_panda.py (which builds one combined
transfer.api.globus.org token covering several endpoints), this script targets a
single site's Globus collection directly: the resulting refresh token's resource
server is the collection ID itself, scoped only to that collection's "https" and
"data_access" scopes.

This runs two interactive Globus login flows:
  1) a basic transfer.api.globus.org login, used only to search the Transfer
     service for the site's collection ID and https_server base URL
  2) a second login requesting that collection's https/data_access scopes; the
     refresh token from this second login is what gets stored

The result is written to --output (default <site_name>_https_token.yaml):

    client_id: <client_id>
    refresh_token: <refresh_token>
    https_server: <https_server_url>

...and also stored in Panda under --panda_secret_key as a JSON object:
    {"client_id": <client_id>, "refresh_token": <refresh_token>, "https_server": <https_server_url>}

Required packages:
  pip install panda-client globus-sdk

Usage:
  python examples/hpc/generate_https_token_to_panda.py --site NERSC --panda_secret_key NERSC_HTTPS_TOKEN
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import globus_sdk

try:
    # pandaclient is optional in examples; import when available
    from pandaclient import Client
except Exception:  # pragma: no cover - example
    Client = None


DEFAULT_CLIENT_ID = "165ae151-86aa-430a-8251-ce565e51998f"  # USATLAS_HPC_Globus
BASE_TRANSFER_SCOPE = "urn:globus:auth:scope:transfer.api.globus.org:all"

# site name -> (endpoint_search fulltext query, index of the desired result)
SITE_QUERIES = {
    "NERSC": ("NERSC DTN", 0),
    "SDCC": ("SDCC", 0),  # bump to 1 or 2 if wrong
    "CRUX": ("alcf#dtn_eagle", 0),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a Globus HTTPS access token for one site and store it in Panda user secrets")
    parser.add_argument("--site", required=True, choices=sorted(SITE_QUERIES), help="Site to generate an https token for")
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID, help="Globus native app client ID")
    parser.add_argument("--panda_secret_key", required=True, help="Panda secret key name to store the token under")
    parser.add_argument("--output", type=Path, default=None, help="Path to also write refresh_token/https_server (default <site>_https_token.yaml)")
    return parser.parse_args()


def interactive_login(client, requested_scopes, *, refresh_tokens=False):
    client.oauth2_start_flow(requested_scopes=requested_scopes, refresh_tokens=refresh_tokens)
    authorize_url = client.oauth2_get_authorize_url()
    print(f"\nPlease go to this URL and login:\n{authorize_url}\n")
    auth_code = input("Enter the auth code: ").strip()
    if not auth_code:
        raise RuntimeError("No authorization code entered")
    return client.oauth2_exchange_code_for_tokens(auth_code)


def find_collection(tc, query, pick=0, show_candidates=True):
    """Search for a collection/endpoint and return its UUID."""
    results = list(tc.endpoint_search(filter_fulltext=query, limit=5))
    if not results:
        raise RuntimeError(f"No endpoint found for query: '{query}'")
    if show_candidates:
        print(f"  Candidates for '{query}':")
        for i, ep in enumerate(results):
            marker = ">>>" if i == pick else "   "
            print(f"  {marker} [{i}] '{ep['display_name']}' ({ep['id']})")
    return results[pick]["id"]


def main() -> None:
    args = parse_args()
    output = args.output or Path.cwd() / f"{args.site}_https_token.yaml"
    query, pick = SITE_QUERIES[args.site]
    client = globus_sdk.NativeAppAuthClient(args.client_id)

    # Step 1: basic transfer token (no data_access yet), just to search for the collection
    try:
        token_response = interactive_login(client, [BASE_TRANSFER_SCOPE])
    except Exception as exc:
        print(f"Error obtaining basic transfer token: {exc}")
        sys.exit(1)
    transfer_token = token_response.by_resource_server["transfer.api.globus.org"]["access_token"]

    tc = globus_sdk.TransferClient(authorizer=globus_sdk.AccessTokenAuthorizer(transfer_token))
    try:
        collection_id = find_collection(tc, query, pick=pick)
        https_server = tc.get_endpoint(collection_id).get("https_server")
    except RuntimeError as exc:
        print(f"Error finding collection for site {args.site}: {exc}")
        sys.exit(2)
    if not https_server:
        print(f"WARNING: collection '{collection_id}' for site {args.site} has no https_server")

    # Step 2: re-run the flow scoped to this collection's https/data_access scopes
    scopes = [
        f"https://auth.globus.org/scopes/{collection_id}/https",
        f"https://auth.globus.org/scopes/{collection_id}/data_access",
    ]
    print(
        f"\nNOTE: please log in with the identity that has access to {args.site} for this step.\n"
        f"If your Globus session is already authorized under a different site's login, the\n"
        f"resulting token will not have permission to download files from {args.site}.\n"
        "You may need to log out of Globus first (https://auth.globus.org/logout) before\n"
        "opening the URL below, then log back in as the right identity.\n"
    )
    try:
        token_response = interactive_login(client, scopes, refresh_tokens=True)
    except Exception as exc:
        print(f"Error obtaining https token: {exc}")
        sys.exit(3)
    refresh_token = token_response.by_resource_server[collection_id].get("refresh_token")
    if not refresh_token:
        print("No refresh_token found in token data")
        sys.exit(4)

    content = f"client_id: {args.client_id}\nrefresh_token: {refresh_token}\nhttps_server: {https_server}\n"
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        print(f"Wrote {output}")
    except Exception as exc:
        print(f"Failed to write {output}: {exc}")
        sys.exit(5)

    if Client is None:
        print("pandaclient not available; cannot set Panda secret. Install pandaclient or run this in an environment with it.")
        sys.exit(6)

    secret_value = json.dumps({"client_id": args.client_id, "refresh_token": refresh_token, "https_server": https_server})
    status, (success, message) = Client.set_user_secret(args.panda_secret_key, secret_value)
    if status != 0 or not success:
        print(f"Failed to set Panda secret: status={status} message={message}")
        sys.exit(7)
    print(f"Set Panda user secret '{args.panda_secret_key}' successfully.")


if __name__ == "__main__":
    main()
