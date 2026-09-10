#!/usr/bin/env python3
"""Manage a Globus Transfer refresh token (with data_access scopes for a set of
HPC endpoints) across three steps:

1) --generate_refresh_token (run manually, interactively)
   Runs two interactive Globus login flows:
     a) a basic transfer.api.globus.org login, used only to search the
        Transfer service for the endpoint UUIDs of the facilities listed in
        ENDPOINT_QUERIES below
     b) a second login requesting the full scope (the base transfer scope
        plus data_access/https scopes for every discovered endpoint)
   The resulting refresh token has a long lifetime and is written ONLY to
   --output (default ./usatlas_hpc_globus_transfer_refresh.yaml). It is never
   sent to Panda -- keep this file local and readable only by its owner.

     python examples/hpc/generate_transfer_token_to_panda.py --generate_refresh_token \\
         --output /opt/harvester/etc/panda/usatlas_hpc_globus_transfer_refresh.yaml

2) --generate_access_token (safe to run unattended, e.g. from cron)
   Reads the refresh token from --input, exchanges it for a short-lived
   Globus access token, writes the access token to --output, and uploads it
   to Panda under --panda_secret_key. This can run on a private machine, or
   directly on the harvester machine -- in which case --output can point at
   the file harvester reads, and step 3 below is not needed.

     python examples/hpc/generate_transfer_token_to_panda.py --generate_access_token \\
         --input /opt/harvester/etc/panda/usatlas_hpc_globus_transfer_refresh.yaml \\
         --output /opt/harvester/etc/panda/usatlas_hpc_globus_transfer_access.yaml \\
         --panda_secret_key USATLAS_HPC_TRANSFER_TOKEN

3) --get_access_token (run on the harvester machine)
   Downloads the access token previously uploaded to Panda (in step 2) under
   --panda_secret_key and writes it to --output.

     python examples/hpc/generate_transfer_token_to_panda.py --get_access_token \\
         --output /opt/harvester/etc/panda/usatlas_hpc_globus_transfer_access.yaml \\
         --panda_secret_key USATLAS_HPC_TRANSFER_TOKEN

The refresh token file (step 1) looks like:

    client_id: <client_id>
    refresh_token: <refresh_token>
    NERSC_https: <https_server_url>
    SDCC_https: <https_server_url>
    Crux_https: <https_server_url>

The access token file (steps 2 and 3) looks like:

    client_id: <client_id>
    access_token: <access_token>
    expires_at: <unix_timestamp>
    NERSC_https: <https_server_url>
    SDCC_https: <https_server_url>
    Crux_https: <https_server_url>

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
TRANSFER_RESOURCE_SERVER = "transfer.api.globus.org"

DEFAULT_REFRESH_TOKEN_OUTPUT = Path.cwd() / "usatlas_hpc_globus_transfer_refresh.yaml"
DEFAULT_ACCESS_TOKEN_OUTPUT = Path.cwd() / "usatlas_hpc_globus_transfer_access.yaml"

# label -> (endpoint_search fulltext query, index of the desired result)
ENDPOINT_QUERIES = {
    "NERSC": ("NERSC DTN", 0),
    "SDCC": ("SDCC", 0),  # bump to 1 or 2 if wrong
    # "LCRC": ("LCRC Improv DTN", 0),
    # "TACC Stampede3": ("TACC Stampede3 GCS v5.4 Filesystems", 0),
    "Crux": ("alcf#dtn_eagle", 0),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate/refresh a Globus Transfer token with data_access scopes and pass it through Panda user secrets")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--generate_refresh_token", action="store_true", help="Step 1 (manual, interactive): generate a long-lived refresh token and write it to --output. Never uploaded to Panda.")
    mode.add_argument("--generate_access_token", action="store_true", help="Step 2 (can run unattended): read the refresh token from --input, exchange it for a short-lived access token, write it to --output, and upload it to Panda under --panda_secret_key.")
    mode.add_argument("--get_access_token", action="store_true", help="Step 3 (run on the harvester machine): download the access token from Panda under --panda_secret_key and write it to --output.")

    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID, help="Globus native app client ID (only used with --generate_refresh_token)")
    parser.add_argument("--input", type=Path, default=None, help="Path to the refresh token yaml written by --generate_refresh_token (required for --generate_access_token)")
    parser.add_argument("--output", type=Path, default=None, help="Path to write the resulting yaml file (default depends on the selected step)")
    parser.add_argument("--panda_secret_key", default=None, help="Panda secret key name to store/retrieve the access token under (required for --generate_access_token and --get_access_token)")
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


def endpoints_from_refresh_yaml(data: dict) -> dict:
    """Extract {label: https_server} from a parsed refresh/access token yaml."""
    return {key[: -len("_https")]: value for key, value in data.items() if key.endswith("_https")}


def build_access_token_yaml_data(client_id: str, access_token: str, expires_at, endpoints: dict) -> dict:
    result = {"client_id": client_id, "access_token": access_token, "expires_at": expires_at}
    for label, https_server in endpoints.items():
        result[f"{label}_https"] = https_server
    return result


def cmd_generate_refresh_token(args: argparse.Namespace) -> None:
    output = args.output or DEFAULT_REFRESH_TOKEN_OUTPUT
    client = globus_sdk.NativeAppAuthClient(args.client_id)

    # Step 1: basic transfer token (no data_access yet), just to search endpoints
    try:
        token_response = interactive_login(client, [BASE_TRANSFER_SCOPE])
    except Exception as exc:
        print(f"Error obtaining basic transfer token: {exc}")
        sys.exit(1)
    transfer_token = token_response.by_resource_server[TRANSFER_RESOURCE_SERVER]["access_token"]

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
    refresh_token = token_response.by_resource_server[TRANSFER_RESOURCE_SERVER].get("refresh_token")
    if not refresh_token:
        print("No refresh_token found in token data")
        sys.exit(4)

    data = {"client_id": args.client_id, "refresh_token": refresh_token}
    for label, info in endpoints.items():
        data[f"{label}_https"] = info["https_server"]
    try:
        write_key_value_yaml(output, data)
        print(f"Wrote refresh token to {output}. Keep this file local; it is not uploaded to Panda.")
    except Exception as exc:
        print(f"Failed to write {output}: {exc}")
        sys.exit(5)


def cmd_generate_access_token(args: argparse.Namespace) -> None:
    output = args.output or DEFAULT_ACCESS_TOKEN_OUTPUT

    try:
        refresh_data = read_key_value_yaml(args.input)
    except Exception as exc:
        print(f"Failed to read {args.input}: {exc}")
        sys.exit(1)
    client_id = refresh_data.get("client_id")
    refresh_token = refresh_data.get("refresh_token")
    if not client_id or not refresh_token:
        print(f"{args.input} is missing client_id or refresh_token")
        sys.exit(2)
    endpoints = endpoints_from_refresh_yaml(refresh_data)

    client = globus_sdk.NativeAppAuthClient(client_id)
    try:
        token_response = client.oauth2_refresh_token(refresh_token)
    except Exception as exc:
        print(f"Error refreshing transfer token: {exc}")
        sys.exit(3)
    token_data = token_response.by_resource_server.get(TRANSFER_RESOURCE_SERVER, {})
    access_token = token_data.get("access_token")
    if not access_token:
        print("No access_token found in refreshed token data")
        sys.exit(4)
    expires_at = token_data.get("expires_at_seconds")

    try:
        write_key_value_yaml(output, build_access_token_yaml_data(client_id, access_token, expires_at, endpoints))
        print(f"Wrote access token to {output}")
    except Exception as exc:
        print(f"Failed to write {output}: {exc}")
        sys.exit(5)

    if Client is None:
        print("pandaclient not available; cannot set Panda secret. Install pandaclient or run this in an environment with it.")
        sys.exit(6)

    secret_value = json.dumps({"client_id": client_id, "access_token": access_token, "expires_at": expires_at, "endpoints": endpoints})
    status, (success, message) = Client.set_user_secret(args.panda_secret_key, secret_value)
    if status != 0 or not success:
        print(f"Failed to set Panda secret: status={status} message={message}")
        sys.exit(7)
    print(f"Set Panda user secret '{args.panda_secret_key}' successfully.")


def cmd_get_access_token(args: argparse.Namespace) -> None:
    output = args.output or DEFAULT_ACCESS_TOKEN_OUTPUT

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
        data = build_access_token_yaml_data(payload.get("client_id"), payload.get("access_token"), payload.get("expires_at"), payload.get("endpoints", {}))
        write_key_value_yaml(output, data)
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
