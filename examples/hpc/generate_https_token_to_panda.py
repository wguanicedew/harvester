#!/usr/bin/env python3
"""Manage a Globus HTTPS access token for a single HPC site across three steps.

Unlike generate_transfer_token_to_panda.py (which builds one combined
transfer.api.globus.org token covering several endpoints), this script targets
a single site's Globus collection directly: the resulting token's resource
server is the collection ID itself, scoped only to that collection's "https"
and "data_access" scopes.

1) --generate_refresh_token (run manually, interactively)
   Runs two interactive Globus login flows:
     a) a basic transfer.api.globus.org login, used only to search the
        Transfer service for the site's collection ID and https_server base
        URL
     b) a second login requesting that collection's https/data_access
        scopes; the refresh token from this second login is what gets
        written
   The resulting refresh token has a long lifetime and is written ONLY to
   --output. It is never sent to Panda -- keep this file local and readable
   only by its owner.

     python examples/hpc/generate_https_token_to_panda.py --generate_refresh_token --site NERSC \\
         --output /opt/harvester/etc/panda/NERSC_https_refresh_token.yaml

2) --generate_access_token (safe to run unattended, e.g. from cron)
   Reads the refresh token from --input, exchanges it for a short-lived
   Globus access token, writes the access token to --output, and uploads it
   to Panda under --panda_secret_key. This can run on a private machine, or
   directly on the harvester machine -- in which case --output can point at
   the file harvester reads, and step 3 below is not needed.

     python examples/hpc/generate_https_token_to_panda.py --generate_access_token --site NERSC \\
         --input /opt/harvester/etc/panda/NERSC_https_refresh_token.yaml \\
         --output /opt/harvester/etc/panda/NERSC_https_access_token.yaml \\
         --panda_secret_key NERSC_HTTPS_TOKEN

3) --get_access_token (run on the harvester machine)
   Downloads the access token previously uploaded to Panda (in step 2) under
   --panda_secret_key and writes it to --output.

     python examples/hpc/generate_https_token_to_panda.py --get_access_token --site NERSC \\
         --output /opt/harvester/etc/panda/NERSC_https_access_token.yaml \\
         --panda_secret_key NERSC_HTTPS_TOKEN

The refresh token file (step 1) looks like:

    client_id: <client_id>
    refresh_token: <refresh_token>
    collection_id: <collection_uuid>
    https_server: <https_server_url>

The access token file (steps 2 and 3) looks like:

    client_id: <client_id>
    access_token: <access_token>
    expires_at: <unix_timestamp>
    collection_id: <collection_uuid>
    https_server: <https_server_url>

Required packages:
  pip install panda-client globus-sdk
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
    parser = argparse.ArgumentParser(description="Generate/refresh a Globus HTTPS access token for one site and pass it through Panda user secrets")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--generate_refresh_token", action="store_true", help="Step 1 (manual, interactive): generate a long-lived refresh token and write it to --output. Never uploaded to Panda.")
    mode.add_argument("--generate_access_token", action="store_true", help="Step 2 (can run unattended): read the refresh token from --input, exchange it for a short-lived access token, write it to --output, and upload it to Panda under --panda_secret_key.")
    mode.add_argument("--get_access_token", action="store_true", help="Step 3 (run on the harvester machine): download the access token from Panda under --panda_secret_key and write it to --output.")

    parser.add_argument("--site", required=True, choices=sorted(SITE_QUERIES), help="Site to manage an https token for")
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID, help="Globus native app client ID (only used with --generate_refresh_token)")
    parser.add_argument("--input", type=Path, default=None, help="Path to the refresh token yaml written by --generate_refresh_token (required for --generate_access_token)")
    parser.add_argument("--panda_secret_key", default=None, help="Panda secret key name to store/retrieve the access token under (required for --generate_access_token and --get_access_token)")
    parser.add_argument("--output", type=Path, default=None, help="Path to write the resulting yaml file (default depends on the selected step and --site)")
    args = parser.parse_args()

    if args.generate_access_token and args.input is None:
        parser.error("--input is required with --generate_access_token")
    if (args.generate_access_token or args.get_access_token) and not args.panda_secret_key:
        parser.error("--panda_secret_key is required with --generate_access_token and --get_access_token")
    return args


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


def read_key_value_yaml(path: Path) -> dict:
    """Parse the flat 'key: value' yaml files written by this script."""
    data = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip()
    return data


def write_key_value_yaml(path: Path, data: dict) -> None:
    lines = [f"{key}: {value}" for key, value in data.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def default_output(site: str, suffix: str) -> Path:
    return Path.cwd() / f"{site}_https_{suffix}.yaml"


def cmd_generate_refresh_token(args: argparse.Namespace) -> None:
    output = args.output or default_output(args.site, "refresh_token")
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

    data = {"client_id": args.client_id, "refresh_token": refresh_token, "collection_id": collection_id, "https_server": https_server}
    try:
        write_key_value_yaml(output, data)
        print(f"Wrote refresh token to {output}. Keep this file local; it is not uploaded to Panda.")
    except Exception as exc:
        print(f"Failed to write {output}: {exc}")
        sys.exit(5)


def cmd_generate_access_token(args: argparse.Namespace) -> None:
    output = args.output or default_output(args.site, "access_token")

    try:
        refresh_data = read_key_value_yaml(args.input)
    except Exception as exc:
        print(f"Failed to read {args.input}: {exc}")
        sys.exit(1)
    client_id = refresh_data.get("client_id")
    refresh_token = refresh_data.get("refresh_token")
    collection_id = refresh_data.get("collection_id")
    https_server = refresh_data.get("https_server")
    if not client_id or not refresh_token or not collection_id:
        print(f"{args.input} is missing client_id, refresh_token, or collection_id")
        sys.exit(2)

    client = globus_sdk.NativeAppAuthClient(client_id)
    try:
        token_response = client.oauth2_refresh_token(refresh_token)
    except Exception as exc:
        print(f"Error refreshing https token: {exc}")
        sys.exit(3)
    token_data = token_response.by_resource_server.get(collection_id, {})
    access_token = token_data.get("access_token")
    if not access_token:
        print("No access_token found in refreshed token data")
        sys.exit(4)
    expires_at = token_data.get("expires_at_seconds")

    data = {"client_id": client_id, "access_token": access_token, "expires_at": expires_at, "collection_id": collection_id, "https_server": https_server}
    try:
        write_key_value_yaml(output, data)
        print(f"Wrote access token to {output}")
    except Exception as exc:
        print(f"Failed to write {output}: {exc}")
        sys.exit(5)

    if Client is None:
        print("pandaclient not available; cannot set Panda secret. Install pandaclient or run this in an environment with it.")
        sys.exit(6)

    secret_value = json.dumps(data)
    status, (success, message) = Client.set_user_secret(args.panda_secret_key, secret_value)
    if status != 0 or not success:
        print(f"Failed to set Panda secret: status={status} message={message}")
        sys.exit(7)
    print(f"Set Panda user secret '{args.panda_secret_key}' successfully.")


def cmd_get_access_token(args: argparse.Namespace) -> None:
    output = args.output or default_output(args.site, "access_token")

    if Client is None:
        print("pandaclient not available; cannot get Panda secret. Install pandaclient or run this in an environment with it.")
        sys.exit(1)

    status, (success, secrets) = Client.get_user_secrets()
    if status != 0 or not success:
        print(f"Failed to get Panda user secrets: status={status} data={secrets}")
        sys.exit(2)

    raw = secrets.get(args.panda_secret_key)
    if not raw:
        print(f"Panda secret '{args.panda_secret_key}' is empty")
        sys.exit(3)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Panda secret '{args.panda_secret_key}' is not valid JSON: {exc}")
        sys.exit(4)

    try:
        write_key_value_yaml(output, payload)
        print(f"Wrote access token to {output}")
    except Exception as exc:
        print(f"Failed to write {output}: {exc}")
        sys.exit(5)


def main() -> None:
    args = parse_args()
    if args.generate_refresh_token:
        cmd_generate_refresh_token(args)
    elif args.generate_access_token:
        cmd_generate_access_token(args)
    elif args.get_access_token:
        cmd_get_access_token(args)


if __name__ == "__main__":
    main()
