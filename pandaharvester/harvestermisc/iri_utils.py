"""Pure-Python IRI API client configured from a YAML file.

Adapted from iri-client's ``src/python/client.py`` for use inside Harvester
submitter/monitor/sweeper plugins.

Config file format (YAML):

    base_url: https://api.iri.nersc.gov   # optional, defaults to NERSC server
    access_token: <your_token>            # required for authenticated endpoints
    resource_id: <default_resource_id>    # optional default for all operations

Config resolution order (when no path is passed to IriClient()):
    1. explicit ``base_url``/``access_token``/``resource_id`` keyword arguments
    2. $IRI_CLIENT_CONFIG environment variable
    3. ~/.iri.yaml
"""

import json as _json
import os
import shlex
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
import yaml

DEFAULT_BASE_URL = "https://api.iri.nersc.gov"
_DEFAULT_CONFIG_PATH = Path.home() / ".iri.yaml"
_TASK_TERMINAL_STATES = {"completed", "failed", "canceled"}
_TASK_POLL_INTERVAL = 5  # seconds between task status polls
_TASK_MAX_POLLS = 60  # ~5 minutes at 5s interval


def _stream_to_file(resp, dest):
    with open(dest, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=65536):
            fh.write(chunk)


class IriClientError(Exception):
    pass


class IriClient:
    """Synchronous IRI API client backed by a YAML config file."""

    def __init__(self, config_path=None, *, base_url=None, access_token=None, resource_id=None, debug=False):
        config = {}
        if config_path is not None or (base_url is None and access_token is None):
            config = _load_config(_resolve_config_path(config_path))
        self._base_url = (base_url or config.get("base_url", DEFAULT_BASE_URL)).rstrip("/")
        self._resource_id = resource_id or config.get("resource_id")
        self._debug = debug
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"
        token = access_token or config.get("access_token")
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------

    def launch_job(self, job_spec, *, resource_id=None):
        """Submit a job to a compute resource.

        POST /api/v1/compute/job/{resource_id}

        Args:
            job_spec: Job specification dict (executable, arguments, resources, etc.).
            resource_id: Compute resource ID. Falls back to config ``resource_id``.

        Returns:
            Created job object including ``id``.
        """
        rid = self._resource(resource_id)
        url = f"{self._base_url}/api/v1/compute/job/{_encode(rid)}"
        self._curl("POST", url, json_body=job_spec)
        resp = self._session.post(url, json=job_spec)
        return self._fetch(resp)

    def get_job(self, job_id, *, resource_id=None):
        """Get status of a submitted job.

        GET /api/v1/compute/status/{resource_id}/{job_id}

        Args:
            job_id: Job identifier returned by :meth:`launch_job`.
            resource_id: Compute resource ID. Falls back to config ``resource_id``.

        Returns:
            Job object with status information.
        """
        rid = self._resource(resource_id)
        url = f"{self._base_url}/api/v1/compute/status/{_encode(rid)}/{_encode(job_id)}"
        self._curl("GET", url)
        resp = self._session.get(url)
        return self._fetch(resp)

    def get_jobs(self, *, resource_id=None, filters=None, offset=None, limit=None, historical=False, include_spec=False):
        """List jobs on a compute resource.

        POST /api/v1/compute/status/{resource_id}

        Args:
            resource_id: Compute resource ID. Falls back to config ``resource_id``.
            filters: Optional filter object passed as the request body.
            offset: Pagination offset.
            limit: Maximum number of jobs to return.
            historical: Include completed/historical jobs.
            include_spec: Include job specification in each result.

        Returns:
            List of job objects.
        """
        rid = self._resource(resource_id)
        url = f"{self._base_url}/api/v1/compute/status/{_encode(rid)}"
        params = {}
        if offset is not None:
            params["offset"] = str(offset)
        if limit is not None:
            params["limit"] = str(limit)
        if historical:
            params["historical"] = "true"
        if include_spec:
            params["include_spec"] = "true"
        self._curl("POST", url, params=params, json_body=filters or {})
        resp = self._session.post(url, params=params, json=filters)
        return self._fetch(resp)

    def cancel_job(self, job_id, *, resource_id=None):
        """Cancel a submitted job.

        DELETE /api/v1/compute/cancel/{resource_id}/{job_id}

        Args:
            job_id: Job identifier returned by :meth:`launch_job`.
            resource_id: Compute resource ID. Falls back to config ``resource_id``.

        Returns:
            Result object from the API (empty dict on a bare 204 response).
        """
        rid = self._resource(resource_id)
        url = f"{self._base_url}/api/v1/compute/cancel/{_encode(rid)}/{_encode(job_id)}"
        self._curl("DELETE", url)
        resp = self._session.delete(url)
        return self._fetch(resp)

    # ------------------------------------------------------------------
    # Archive helpers
    # ------------------------------------------------------------------

    def create_input_archive(self, work_dir, inputs):
        """Create a tar.gz archive of a job's input files.
        If the inputs is a dictionary, the keys are used as the names of the files in the archive.
        If the inputs is a list, the files are added to the archive with their base names

        Args:
            work_dir: Working directory for the job (typically the worker's
                access point). The archive is written here.
            inputs: A dictionary mapping input names to their file paths or a list of file paths.
                (e.g., the X509 proxy,token file, batch script, job data file). Missing or falsy
                entries are skipped.

        Returns:
            Path to the created archive.
        """
        archive_path = os.path.join(work_dir, "input.tar.gz")

        with tarfile.open(archive_path, "w:gz") as tar:
            if isinstance(inputs, dict):
                for name, file_path in inputs.items():
                    if file_path and os.path.exists(file_path):
                        tar.add(file_path, arcname=name)
            else:
                for file_path in inputs:
                    if file_path and os.path.exists(file_path):
                        tar.add(file_path, arcname=os.path.basename(file_path))
        return archive_path

    # ------------------------------------------------------------------
    # HTTP export (htaccess/htpasswd)
    # ------------------------------------------------------------------

    def download_from_http(self, url, local_dest, *, username=None, password=None):
        """Download a file from a plain HTTP(S) URL, such as a webserver export
        directory protected by htaccess/htpasswd Basic authentication.

        This does not go through the IRI API and does not use the client's
        bearer token; it issues a direct HTTP GET with optional Basic auth.

        Args:
            url: Full URL of the remote file to download.
            local_dest: Local destination path for the downloaded file.
            username: htaccess Basic auth username. Omit for an unprotected URL.
            password: htaccess Basic auth password (htpasswd).
        """
        auth = (username, password) if username is not None and password is not None else None
        resp = requests.get(url, auth=auth, stream=True)
        _raise_for_error(resp)
        _stream_to_file(resp, local_dest)

    # ------------------------------------------------------------------
    # Filesystem
    # ------------------------------------------------------------------

    def stat(self, path, *, resource_id=None, dereference=False):
        """Get metadata for a file or directory.

        GET /api/v1/filesystem/stat/{resource_id}?path=...

        Args:
            path: Absolute path on the remote filesystem.
            resource_id: Filesystem resource ID. Falls back to config ``resource_id``.
            dereference: If ``True``, follow symbolic links.

        Returns:
            Stat object (name, size, type, permissions, owner, …).
        """
        rid = self._resource(resource_id)
        params = {"path": path}
        if dereference:
            params["dereference"] = "true"
        url = f"{self._base_url}/api/v1/filesystem/stat/{_encode(rid)}"
        self._curl("GET", url, params=params)
        resp = self._session.get(url, params=params)
        return self._fetch(resp)

    def ls(self, path, *, resource_id=None, show_hidden=False, numeric_uid=False, recursive=False, dereference=False):
        """List directory contents.

        GET /api/v1/filesystem/ls/{resource_id}?path=...

        Args:
            path: Absolute path to the directory on the remote filesystem.
            resource_id: Filesystem resource ID. Falls back to config ``resource_id``.
            show_hidden: Include entries whose name begins with ``'.'``.
            numeric_uid: Show numeric UID/GID instead of names.
            recursive: List subdirectories recursively.
            dereference: Follow symbolic links.

        Returns:
            Directory listing object.
        """
        rid = self._resource(resource_id)
        params = {"path": path}
        if show_hidden:
            params["showHidden"] = "true"
        if numeric_uid:
            params["numericUid"] = "true"
        if recursive:
            params["recursive"] = "true"
        if dereference:
            params["dereference"] = "true"
        url = f"{self._base_url}/api/v1/filesystem/ls/{_encode(rid)}"
        self._curl("GET", url, params=params)
        resp = self._session.get(url, params=params)
        return self._fetch(resp)

    def download(self, remote_path, local_dest, *, resource_id=None):
        """Download a file from the remote filesystem.

        GET /api/v1/filesystem/download/{resource_id}?path=...

        The server may respond immediately with binary content or return a task
        (``task_id`` / ``task_uri``).  In the task case the method polls until
        the task completes and then streams the file from the URL found in
        ``task.result``.

        Args:
            remote_path: Absolute path to the file on the remote filesystem.
            local_dest: Local destination path for the downloaded file.
            resource_id: Filesystem resource ID. Falls back to config ``resource_id``.
        """
        rid = self._resource(resource_id)
        url = f"{self._base_url}/api/v1/filesystem/download/{_encode(rid)}"
        params = {"path": remote_path}
        self._curl("GET", url, params=params, output_path=local_dest)
        resp = self._session.get(url, params=params, stream=True)
        _raise_for_error(resp)

        if "application/json" in resp.headers.get("Content-Type", ""):
            data = _json_response(resp)
            if "task_id" not in data or "task_uri" not in data:
                raise IriClientError(f"Unexpected JSON response from download: {data}")
            task = self._wait_for_task(data["task_id"], data["task_uri"])
            result = task.get("result") or {}
            if self._debug:
                print(f"# download task {data['task_id']} completed", file=sys.stderr)
            if "output" not in result:
                raise IriClientError(f"Download task completed but result contains no output: {result}")
            output = result["output"]
            with open(local_dest, "w" if isinstance(output, str) else "wb") as fh:
                fh.write(output)
        else:
            _stream_to_file(resp, local_dest)

    def upload(self, local_path, remote_path, *, resource_id=None):
        """Upload a local file to the remote filesystem.

        POST /api/v1/filesystem/upload/{resource_id}?path=...

        Uses ``multipart/form-data`` with the field name ``file``.

        Args:
            local_path: Local path of the file to upload.
            remote_path: Destination absolute path on the remote filesystem.
            resource_id: Filesystem resource ID. Falls back to config ``resource_id``.

        Returns:
            Task or result object returned by the API.
        """
        rid = self._resource(resource_id)
        url = f"{self._base_url}/api/v1/filesystem/upload/{_encode(rid)}"
        params = {"path": remote_path}
        self._curl("POST", url, params=params, upload_path=local_path)
        with open(local_path, "rb") as fh:
            resp = self._session.post(url, params=params, files={"file": fh})
        return self._fetch(resp)

    def mkdir(self, path, *, resource_id=None, parents=False):
        """Create a directory on the remote filesystem.

        POST /api/v1/filesystem/mkdir/{resource_id}

        Args:
            path: Absolute path of the directory to create.
            resource_id: Filesystem resource ID. Falls back to config ``resource_id``.
            parents: Create parent directories as needed (like ``mkdir -p``).

        Returns:
            Result object from the API.
        """
        rid = self._resource(resource_id)
        url = f"{self._base_url}/api/v1/filesystem/mkdir/{_encode(rid)}"
        body = {"path": path, "parent": parents}
        self._curl("POST", url, json_body=body)
        resp = self._session.post(url, json=body)
        return self._fetch(resp)

    def mv(self, path, target_path, *, resource_id=None):
        """Move or rename a file or directory.

        POST /api/v1/filesystem/mv/{resource_id}

        Args:
            path: Source absolute path on the remote filesystem.
            target_path: Destination absolute path on the remote filesystem.
            resource_id: Filesystem resource ID. Falls back to config ``resource_id``.

        Returns:
            Result object from the API.
        """
        rid = self._resource(resource_id)
        url = f"{self._base_url}/api/v1/filesystem/mv/{_encode(rid)}"
        body = {"path": path, "target_path": target_path}
        self._curl("POST", url, json_body=body)
        resp = self._session.post(url, json=body)
        return self._fetch(resp)

    def chown(self, path, *, resource_id=None, owner="", group=""):
        """Change the owner and/or group of a file or directory.

        PUT /api/v1/filesystem/chown/{resource_id}

        Args:
            path: Absolute path on the remote filesystem.
            resource_id: Filesystem resource ID. Falls back to config ``resource_id``.
            owner: New user owner name (leave empty to keep current).
            group: New group owner name (leave empty to keep current).

        Returns:
            Result object from the API.
        """
        rid = self._resource(resource_id)
        url = f"{self._base_url}/api/v1/filesystem/chown/{_encode(rid)}"
        body = {"path": path, "owner": owner, "group": group}
        self._curl("PUT", url, json_body=body)
        resp = self._session.put(url, json=body)
        return self._fetch(resp)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _curl(self, method, url, *, params=None, json_body=None, upload_path=None, output_path=None):
        if not self._debug:
            return
        parts = ["curl", "-s"]
        if method.upper() != "GET":
            parts += ["-X", method.upper()]
        auth = self._session.headers.get("Authorization")
        if auth:
            parts += ["-H", f"Authorization: {auth}"]
        parts += ["-H", "Accept: application/json"]
        if json_body is not None:
            parts += ["-H", "Content-Type: application/json", "-d", _json.dumps(json_body)]
        if upload_path is not None:
            parts += ["-F", f"file=@{upload_path}"]
        if output_path is not None:
            parts += ["-o", str(output_path)]
        full_url = f"{url}?{urlencode(params)}" if params else url
        parts.append(full_url)
        print(shlex.join(parts), file=sys.stderr)

    def _fetch(self, resp):
        result = _json_response(resp)
        if "task_id" in result and "task_uri" in result:
            result = self._wait_for_task(result["task_id"], result["task_uri"])
        return result

    def _wait_for_task(self, task_id, task_uri):
        for attempt in range(1, _TASK_MAX_POLLS + 1):
            self._curl("GET", task_uri)
            resp = self._session.get(task_uri)
            task = _json_response(resp)
            status = task.get("status", "")
            if self._debug:
                print(f"# task {task_id}: status={status}", file=sys.stderr)
            if status in _TASK_TERMINAL_STATES:
                return task
            if attempt < _TASK_MAX_POLLS:
                time.sleep(_TASK_POLL_INTERVAL)
        raise IriClientError(f"Task {task_id} did not reach a terminal state after {_TASK_MAX_POLLS} polls ({_TASK_POLL_INTERVAL}s interval)")

    def _resource(self, resource_id):
        rid = resource_id or self._resource_id
        if not rid:
            raise IriClientError("resource_id is required; provide it as a keyword argument or set 'resource_id' in the config file")
        return rid


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _resolve_config_path(path):
    if path is not None:
        return Path(path).expanduser()
    env = os.environ.get("IRI_CLIENT_CONFIG")
    if env:
        return Path(env).expanduser()
    return _DEFAULT_CONFIG_PATH


def _load_config(path):
    with open(path) as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise IriClientError(f"Config file '{path}' must be a YAML mapping")
    return data


def _encode(segment):
    return quote(str(segment), safe="")


def _json_response(resp):
    _raise_for_error(resp)
    if not resp.content:
        return {}
    return resp.json()


def _raise_for_error(resp):
    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise IriClientError(f"HTTP {resp.status_code}: {detail}")
