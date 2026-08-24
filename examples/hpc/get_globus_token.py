#!/usr/bin/env python3
"""
Minimal vendored copy of get_globus_token functionality used by examples.

Exposes get_tokens(...) which will either refresh saved tokens or perform
an interactive login to obtain tokens. This is a greatly trimmed version
intended for example scripts only.
"""
from __future__ import annotations

import json
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

import globus_sdk
from globus_sdk.exc import GlobusAPIError, GlobusConnectionError

DEFAULT_CLIENT_ID = "fae5c579-490a-4d76-b6eb-d78f65caeb63"
ALCF_CLIENT_ID = "8b84fc2d-49e9-49ea-b54d-b3a29a70cf31"
KNOWN_CLIENT_IDS = (DEFAULT_CLIENT_ID, ALCF_CLIENT_ID)
RESOURCE_SERVER = "auth.globus.org"

FACILITY_SCOPE_MAP = {
    "nersc": {
        "scope": "https://auth.globus.org/scopes/ed3e577d-f7f3-4639-b96e-ff5a8445d699/iri_api",
        "label": "NERSC IRI API",
    },
    "alcf": {
        "scope": "https://auth.globus.org/scopes/6be511f6-a071-471f-9bc0-02a0d0836723/filesystem",
        "label": "ALCF IRI API",
    },
}

REQUIRED_SCOPES = {"openid", "profile", "email", "urn:globus:auth:scope:auth.globus.org:view_identities"}

# --validate-iri support: default endpoints used to sanity-check a facility token.
DEFAULT_IRI_VALIDATE_URL = "https://api.iri.nersc.gov/api/v1/account/projects"
ALCF_BASE_URL = "https://api.alcf.anl.gov"
ALCF_HOME_RESOURCE_ID = "6115bd2c-957a-4543-abff-5fae52992ff2"


def ensure_private_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)


def load_tokens(token_file: Path) -> Optional[Dict]:
    if not token_file.exists():
        return None
    with token_file.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_tokens(token_file: Path, tokens: Dict) -> None:
    ensure_private_parent_dir(token_file)
    tmp = token_file.with_suffix(".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    os.replace(tmp, token_file)
    os.chmod(token_file, stat.S_IRUSR | stat.S_IWUSR)


def get_client_id(facilities: List[str]) -> str:
    if "alcf" in facilities:
        return ALCF_CLIENT_ID
    return DEFAULT_CLIENT_ID


def parse_scope_string(scope_string: str) -> set[str]:
    return set(scope_string.split()) if scope_string else set()


def get_facility_token(token_data: Dict, facility: str) -> Dict:
    scope = FACILITY_SCOPE_MAP[facility]["scope"]
    for token in token_data.get("other_tokens", []):
        if scope in parse_scope_string(token.get("scope", "")):
            return token
    raise RuntimeError(f"Missing token for facility {facility}")


def default_alcf_validate_path() -> str:
    username = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not username:
        raise RuntimeError("Could not determine a default ALCF validation path. Pass --alcf-validate-path /home/<username>/.")
    return f"/home/{username}/"


def build_alcf_ls_validate_url(resource_id: str, path: str) -> str:
    quoted_resource_id = urllib.parse.quote(resource_id, safe="")
    query = urllib.parse.urlencode({"path": path}, quote_via=urllib.parse.quote, safe="/")
    return f"{ALCF_BASE_URL}/api/v1/filesystem/ls/{quoted_resource_id}?{query}"


def get_validate_url(facility: str, iri_validate_url: Optional[str] = None, alcf_resource_id: str = ALCF_HOME_RESOURCE_ID, alcf_path: Optional[str] = None) -> str:
    if iri_validate_url:
        return iri_validate_url
    if facility == "nersc":
        return DEFAULT_IRI_VALIDATE_URL
    if facility == "alcf":
        path = alcf_path or default_alcf_validate_path()
        return build_alcf_ls_validate_url(alcf_resource_id, path)
    raise RuntimeError(f"No default validation endpoint for {facility}")


def validate_iri_token(facility_token_data: Dict, validate_url: str):
    """Call validate_url with the facility access token. Raises RuntimeError on failure."""
    request = urllib.request.Request(
        validate_url,
        headers={
            "accept": "application/json",
            "Authorization": f"Bearer {facility_token_data['access_token']}",
            "User-Agent": "iri-api-client/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request) as response:
            body = response.read().decode("utf-8")
            data = json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        details = body.strip() or exc.reason
        raise RuntimeError(f"IRI validation failed with HTTP {exc.code} from {validate_url}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"IRI validation request failed for {validate_url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"IRI validation returned non-JSON data from {validate_url}") from exc

    if isinstance(data, dict):
        session_info = data.get("session_info")
        if isinstance(session_info, dict):
            authentications = session_info.get("authentications")
            if isinstance(authentications, dict) and not authentications:
                raise RuntimeError(
                    "IRI validation succeeded but session_info.authentications is empty. "
                    "Re-run with --force-login and use a Chrome incognito window."
                )

    return data


def interactive_login(client: globus_sdk.NativeAppAuthClient, facilities: List[str], prompt_login: bool = False) -> Dict:
    client.oauth2_start_flow(requested_scopes=" ".join(sorted(REQUIRED_SCOPES | {FACILITY_SCOPE_MAP[f]["scope"] for f in facilities})), refresh_tokens=True)
    print("Open this URL, login, and consent:")
    print(client.oauth2_get_authorize_url(prompt=("login" if prompt_login else globus_sdk.MISSING)))
    code = input("\nEnter authorization code: ").strip()
    if not code:
        raise RuntimeError("No authorization code entered")
    token_response = client.oauth2_exchange_code_for_tokens(code)
    return token_response.data


def refresh_tokens(client: globus_sdk.NativeAppAuthClient, refresh_token: str) -> Dict:
    token_response = client.oauth2_refresh_token(refresh_token)
    return token_response.data


def refresh_tokens_with_client_ids(refresh_token: str, client_ids: List[str], token_label: str) -> tuple[Optional[Dict], Optional[str]]:
    failures = []
    for client_id in client_ids:
        client = globus_sdk.NativeAppAuthClient(client_id)
        try:
            return refresh_tokens(client, refresh_token), client_id
        except GlobusAPIError as exc:
            failures.append(f"{client_id}: HTTP {exc.http_status}")
        except GlobusConnectionError:
            failures.append(f"{client_id}: connection error")
    print(f"Refresh failed for {token_label} with known Globus client IDs ({'; '.join(failures)}).")
    return None, None


def get_refresh_token(stored_tokens: Dict) -> Optional[str]:
    if "refresh_token" in stored_tokens:
        return stored_tokens.get("refresh_token")
    auth_tokens = stored_tokens.get(RESOURCE_SERVER)
    if isinstance(auth_tokens, dict):
        return auth_tokens.get("refresh_token")
    return None


def get_refresh_token_for_scope(stored_tokens: Dict, scope: str) -> Optional[str]:
    try:
        for token in stored_tokens.get("other_tokens", []):
            if scope in parse_scope_string(token.get("scope", "")):
                return token.get("refresh_token")
    except Exception:
        return None
    return None


def refresh_stored_tokens(stored_tokens: Dict, facilities: List[str], client_ids: List[str]) -> tuple[Optional[Dict], bool, List[str]]:
    refreshed_tokens = dict(stored_tokens)
    used_refresh = False
    used_client_ids: List[str] = []
    auth_refresh_token = get_refresh_token(stored_tokens)
    if auth_refresh_token:
        auth_data, client_id = refresh_tokens_with_client_ids(auth_refresh_token, client_ids, token_label="Globus Auth token")
        if auth_data is not None:
            refreshed_tokens.update(auth_data)
            used_refresh = True
            if client_id and client_id not in used_client_ids:
                used_client_ids.append(client_id)

    for facility in facilities:
        scope = FACILITY_SCOPE_MAP[facility]["scope"]
        refresh_token = get_refresh_token_for_scope(stored_tokens, scope)
        if refresh_token:
            label = FACILITY_SCOPE_MAP[facility]["label"]
            refreshed_token_data, client_id = refresh_tokens_with_client_ids(refresh_token, client_ids, token_label=f"{label} token")
            if refreshed_token_data is not None:
                # replace token for scope
                other_tokens = list(refreshed_tokens.get("other_tokens", []))
                for i, tok in enumerate(other_tokens):
                    if scope in parse_scope_string(tok.get("scope", "")):
                        other_tokens[i] = refreshed_token_data
                        break
                else:
                    other_tokens.append(refreshed_token_data)
                refreshed_tokens["other_tokens"] = other_tokens
                used_refresh = True
                if client_id and client_id not in used_client_ids:
                    used_client_ids.append(client_id)

    # validate presence
    for facility in facilities:
        try:
            get_facility_token(refreshed_tokens, facility)
        except RuntimeError:
            return None, used_refresh, used_client_ids

    if used_refresh:
        return refreshed_tokens, True, used_client_ids
    return None, False, used_client_ids


def get_refresh_client_ids(facilities: List[str]) -> List[str]:
    client_ids = [get_client_id(facilities)]
    for client_id in KNOWN_CLIENT_IDS:
        if client_id not in client_ids:
            client_ids.append(client_id)
    return client_ids


def default_token_file(facilities: List[str]) -> Path:
    if len(facilities) == 1:
        return Path.home() / ".globus" / f"{facilities[0]}_auth_tokens.json"
    return Path.home() / ".globus" / "auth_tokens.json"


def get_tokens(facilities: List[str], token_file: Path | None = None, refresh_only: bool = False, force_login: bool = False, prompt_login: bool = False) -> Dict:
    """Return token data dict. Raises RuntimeError on failure.

    This function attempts to refresh stored tokens (unless force_login) and
    falls back to interactive login when needed. If refresh_only is True and
    refresh fails, raises RuntimeError.
    """
    client_id = get_client_id(facilities)
    client = globus_sdk.NativeAppAuthClient(client_id)

    token_file = token_file or default_token_file(facilities)
    print(f"Using token file: {token_file}")
    stored = load_tokens(token_file)
    auth_data = None
    used_refresh = False
    used_refresh_client_ids: List[str] = []
    if not force_login and stored:
        auth_data, used_refresh, used_refresh_client_ids = refresh_stored_tokens(stored, facilities, get_refresh_client_ids(facilities))

    if auth_data is None:
        if refresh_only:
            facility_labels = ", ".join(FACILITY_SCOPE_MAP[f]["label"] for f in facilities)
            raise RuntimeError(f"Refresh-only mode failed. No usable saved refresh token was found for: {facility_labels}.")
        auth_data = interactive_login(client, facilities, prompt_login=prompt_login)

    # basic validation: ensure facility tokens exist
    for facility in facilities:
        get_facility_token(auth_data, facility)

    save_tokens(token_file, auth_data)
    return auth_data
