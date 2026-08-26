#!/usr/bin/env python3
"""Generate a Globus Transfer refresh token (with data_access scopes for a set of
HPC endpoints) and store it in Panda as a user secret.

This runs two interactive Globus login flows:
  1) a basic transfer.api.globus.org login, used only to search the Transfer
     service for the endpoint UUIDs of the facilities listed in ENDPOINT_QUERIES
     below
  2) a second login requesting the full scope (the base transfer scope plus
     data_access/https scopes for every discovered endpoint); the refresh token
     from this second login is what actually gets stored in Panda, since it is
     the credential needed for unattended/automated transfers later

The refresh token is stored in Panda under --panda_secret_key, and also written
(together with each discovered endpoint's https_server base URL) to --output
(default ./globus_transfer.yaml), e.g.:

    refresh_token: <refresh_token>
    NERSC_https: <https_server_url>
    SDCC_https: <https_server_url>
    Crux_https: <https_server_url>

Required packages:
  pip install panda-client globus-sdk

Usage:
  python examples/hpc/generate_transfer_token_to_panda.py --panda_secret_key USATLAS_HPC_TRANSFER_TOKEN
"""
from __future__ import annotations

import argparse
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

# label -> (endpoint_search fulltext query, index of the desired result)
ENDPOINT_QUERIES = {
    "NERSC": ("NERSC DTN", 0),
    "SDCC": ("SDCC", 0),  # bump to 1 or 2 if wrong
    # "LCRC": ("LCRC Improv DTN", 0),
    # "TACC Stampede3": ("TACC Stampede3 GCS v5.4 Filesystems", 0),
    "Crux": ("alcf#dtn_eagle", 0),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a Globus Transfer refresh token with data_access scopes and store it in Panda user secrets")
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID, help="Globus native app client ID")
    parser.add_argument("--panda_secret_key", required=True, help="Panda secret key name to store the refresh token under")
    parser.add_argument("--output", type=Path, default=Path.cwd() / "usatlas_hpc_globus_transfer.yaml", help="Path to also write the refresh token and per-endpoint https URLs")
    return parser.parse_args()


def interactive_login(client, requested_scopes, *, refresh_tokens=False):
    client.oauth2_start_flow(requested_scopes=requested_scopes, refresh_tokens=refresh_tokens)
    authorize_url = client.oauth2_get_authorize_url()
    print(f"\nPlease go to this URL and login:\n{authorize_url}\n")
    auth_code = input("Enter the auth code: ").strip()
    if not auth_code:
        raise RuntimeError("No authorization code entered")
    return client.oauth2_exchange_code_for_tokens(auth_code)


def find_endpoint(tc, query, pick=0, show_candidates=True):
    """Search for an endpoint and return the UUID of the best match."""
    results = list(tc.endpoint_search(filter_fulltext=query, limit=5))
    if not results:
        raise RuntimeError(f"No endpoint found for query: '{query}'")
    if show_candidates:
        print(f"  Candidates for '{query}':")
        for i, ep in enumerate(results):
            marker = ">>>" if i == pick else "   "
            print(f"  {marker} [{i}] '{ep['display_name']}' ({ep['id']})")
    return results[pick]["id"]


def discover_endpoints(tc):
    """Search for each configured endpoint and fetch its https_server base URL.

    Returns {label: {"id": endpoint_uuid, "https_server": url_or_None}}.
    """
    print("\n-- Discovering endpoints --")
    endpoints = {}
    for label, (query, pick) in ENDPOINT_QUERIES.items():
        try:
            endpoint_id = find_endpoint(tc, query, pick=pick)
            https_server = tc.get_endpoint(endpoint_id).get("https_server")
            if not https_server:
                print(f"  WARNING: endpoint '{label}' ({endpoint_id}) has no https_server")
            endpoints[label] = {"id": endpoint_id, "https_server": https_server}
        except RuntimeError as exc:
            print(f"  WARNING: {exc}")
    return endpoints


def build_full_scope(endpoints):
    data_access_parts = " ".join(
        f"*https://auth.globus.org/scopes/{info['id']}/data_access "
        f"*https://auth.globus.org/scopes/{info['id']}/https"
        for info in endpoints.values()
    )
    return f"{BASE_TRANSFER_SCOPE}[{data_access_parts}]"


def main() -> None:
    args = parse_args()
    client = globus_sdk.NativeAppAuthClient(args.client_id)

    # Step 1: basic transfer token (no data_access yet), just to search endpoints
    try:
        token_response = interactive_login(client, [BASE_TRANSFER_SCOPE])
    except Exception as exc:
        print(f"Error obtaining basic transfer token: {exc}")
        sys.exit(1)
    transfer_token = token_response.by_resource_server["transfer.api.globus.org"]["access_token"]

    # Step 2: search for each configured endpoint by name
    tc = globus_sdk.TransferClient(authorizer=globus_sdk.AccessTokenAuthorizer(transfer_token))
    endpoints = discover_endpoints(tc)
    if not endpoints:
        print("No endpoints discovered; aborting.")
        sys.exit(2)

    # Step 3: build data_access scopes for every discovered endpoint
    full_scope = build_full_scope(endpoints)
    print(f"\n-- Generated scope --\n{full_scope}\n")

    # Step 4: re-run the flow with the full scope to get a refresh token
    try:
        token_response = interactive_login(client, [full_scope], refresh_tokens=True)
    except Exception as exc:
        print(f"Error obtaining full transfer token: {exc}")
        sys.exit(3)
    refresh_token = token_response.by_resource_server["transfer.api.globus.org"].get("refresh_token")
    if not refresh_token:
        print("No refresh_token found in token data")
        sys.exit(4)

    if Client is None:
        print("pandaclient not available; cannot set Panda secret. Install pandaclient or run this in an environment with it.")
        print(f"Refresh token (store manually): {refresh_token}")
        sys.exit(5)

    status, (success, message) = Client.set_user_secret(args.panda_secret_key, refresh_token)
    if status != 0 or not success:
        print(f"Failed to set Panda secret: status={status} message={message}")
        sys.exit(6)
    print(f"Set Panda user secret '{args.panda_secret_key}' successfully.")

    lines = [f"refresh_token: {refresh_token}"]
    for label, info in endpoints.items():
        lines.append(f"{label}_https: {info['https_server']}")
    content = "\n".join(lines) + "\n"
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
        print(f"Wrote {args.output}")
    except Exception as exc:
        print(f"Failed to write {args.output}: {exc}")
        sys.exit(7)


if __name__ == "__main__":
    main()
