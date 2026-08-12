#!/usr/bin/env python3
"""Download an IRI access token from Panda user secrets and write an iri_config.yaml

The script calls pandaclient.get_user_secret to retrieve the secret value stored under
the provided --panda_secret_key and writes an iri_config.yaml at --iri_config.

Required packages:
  pip install panda-client

Example:
  python examples/download_iri_token_from_panda.py --panda_secret_key IRI_ACCESS --iri_config ./iri_config.yaml \\
      --base_url https://api.iri.nersc.gov --resource_id 59e80c79-4dfd-4c53-9c07-7405685fcd37
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import pandaclient
except Exception:  # pragma: no cover - example
    pandaclient = None


DEFAULT_BASE_URL = "https://api.iri.nersc.gov"
DEFAULT_RESOURCE_ID = "59e80c79-4dfd-4c53-9c07-7405685fcd37"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download IRI token from Panda and write iri_config.yaml")
    parser.add_argument("--panda_secret_key", required=True, help="Panda secret key to retrieve")
    parser.add_argument("--iri_config", type=Path, default=Path.cwd() / "iri_config.yaml", help="Path to write iri_config.yaml")
    parser.add_argument("--base_url", default=DEFAULT_BASE_URL, help="IRI API base URL")
    parser.add_argument("--resource_id", default=DEFAULT_RESOURCE_ID, help="IRI resource ID")
    return parser.parse_args()


TEMPLATE = """# IRI API client configuration
# Copy this file to e.g. ~/.iri.yaml and fill in your values.

base_url: {base_url}

resource_id: {resource_id}
access_token: {token}
"""


def main() -> None:
    args = parse_args()
    if pandaclient is None:
        print("pandaclient not available; cannot get Panda secret. Install pandaclient or run in appropriate env.")
        sys.exit(2)

    try:
        token = pandaclient.get_user_secret(args.panda_secret_key)
    except Exception as exc:
        print(f"Failed to get Panda user secret '{args.panda_secret_key}': {exc}")
        sys.exit(3)

    if not token:
        print(f"Panda secret '{args.panda_secret_key}' is empty")
        sys.exit(4)

    content = TEMPLATE.format(token=token, base_url=args.base_url, resource_id=args.resource_id)
    try:
        args.iri_config.parent.mkdir(parents=True, exist_ok=True)
        args.iri_config.write_text(content, encoding="utf-8")
        print(f"Wrote IRI config to {args.iri_config}")
    except Exception as exc:
        print(f"Failed to write IRI config: {exc}")
        sys.exit(5)


if __name__ == "__main__":
    main()
