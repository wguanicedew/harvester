import json
import os

from pandaharvester.harvestercore import core_utils
from pandaharvester.harvestercore.plugin_base import PluginBase
from pandaharvester.harvestercore.work_spec import WorkSpec
from pandaharvester.harvestermisc.iri_utils import GlobusClient, GlobusClientError, IriClient, IriClientError

# logger
baseLogger = core_utils.setup_logger("iri_monitor")

# statuses for which the remote job has stopped running and its output, if any, is ready
_TERMINAL_STATUSES = (WorkSpec.ST_finished, WorkSpec.ST_failed, WorkSpec.ST_cancelled)


def _get_stdio_paths_from_job(job):
    """Extract (stdout_path, stderr_path) from the batch scheduler's admincomment,
    if the IRI job report includes it (e.g. Slurm's stdoutPath/stderrPath). Returns
    (None, None) if unavailable or unparsable.
    """
    meta_data = (job.get("status") or {}).get("meta_data") or {}
    admincomment = meta_data.get("admincomment")
    if not admincomment:
        return None, None
    try:
        comment = json.loads(admincomment) if isinstance(admincomment, str) else admincomment
    except (TypeError, ValueError):
        return None, None
    if not isinstance(comment, dict):
        return None, None
    return comment.get("stdoutPath"), comment.get("stderrPath")


def _strip_globus_prefix(path, globus_download_dir):
    """If path is "<prefix>/<globus_download_dir>/<file>", drop the prefix so the
    result is relative to globus_download_dir, as needed by the Globus HTTPS
    collection whose root differs from the batch scheduler's filesystem root.
    Returns path unchanged if globus_download_dir isn't found in it.
    """
    if not path or not globus_download_dir:
        return path
    idx = path.find(globus_download_dir)
    if idx == -1:
        return path
    return path[idx:]


# monitor for IRI API
class IriMonitor(PluginBase):
    # constructor
    def __init__(self, **kwarg):
        PluginBase.__init__(self, **kwarg)
        self.iri_config = kwarg.get("iri_config")
        self.iri_resource_id = kwarg.get("iri_resource_id")
        self.iri_debug = kwarg.get("iri_debug", False)
        self.iri_client = IriClient(config_path=self.iri_config, resource_id=self.iri_resource_id, debug=self.iri_debug)

        self.remote_work_dir = kwarg.get("remote_work_dir", None)
        self.remote_log_dir = kwarg.get("remote_log_dir", None)

        self.download_logs = kwarg.get("download_logs", False)
        self.download_logs_method = kwarg.get("download_logs_method", "globus_https")

        self.globus_https_config = None
        self.globus_client = None
        self.globus_download_dir = None
        self.remote_export_path = None
        self.htaccess_username = None
        self.htaccess_password = None

        if self.download_logs_method == "globus_https":
            self.globus_https_config = kwarg.get("globus_https_config", None)
            # IRI reports stdout/stderr paths through the HPC's full mounted filesystem path
            # (e.g. under remote_work_dir/remote_log_dir), but the Globus HTTPS collection's
            # root can be mapped to a different, unprefixed path for the same directory.
            # globus_download_dir is that Globus-relative equivalent; when set, it's used both
            # to build the remote log dir and to strip the filesystem prefix off paths reported
            # by the job (see _strip_globus_prefix).
            self.globus_download_dir = kwarg.get("globus_download_dir", None)
            if self.download_logs:
                self.globus_client = GlobusClient(config_path=self.globus_https_config, debug=self.iri_debug)
        elif self.download_logs_method == "remote_export":
            self.remote_export_path = kwarg.get("remote_export_path", None)
            self.htaccess_username = kwarg.get("htaccess_username", None)
            htaccess_password_file = kwarg.get("htaccess_password", None)
            if htaccess_password_file:
                with open(htaccess_password_file) as f:
                    self.htaccess_password = f.read().strip()

        self.logDir = kwarg.get("logDir", None)
        if self.download_logs and self.logDir:
            os.makedirs(self.logDir, exist_ok=True)

    def check_workers(self, workspec_list):
        retList = []


        if self.download_logs_method == "globus_https":
            self.globus_client.reload()  # refresh token may have changed on disk

        self.iri_client.reload()  # refresh token may have changed on disk

        for workSpec in workspec_list:
            # make logger
            tmpLog = self.make_logger(baseLogger, f"workerID={workSpec.workerID}", method_name="check_workers")

            job_id = workSpec.batchID
            if not job_id:
                retList.append((WorkSpec.ST_failed, "no batchID, job is not submitted!"))
                continue

            try:
                job = self.iri_client.get_job(job_id, resource_id=self.iri_resource_id)
            except IriClientError as e:
                retList.append((WorkSpec.ST_failed, f"cannot query IRI job {job_id} due to {e}"))
                continue

            if self.iri_debug:
                tmpLog.debug(f"IRI job status: {job}")

            status = job.get("status") or {}
            batchStatus = (status.get("state") or "").lower()
            exitCode = status.get("exit_code")

            if batchStatus in ["new", "queued"]:
                newStatus = WorkSpec.ST_submitted
            elif batchStatus in ["active"]:
                newStatus = WorkSpec.ST_running
            elif batchStatus in ["completed"]:
                newStatus = WorkSpec.ST_finished if exitCode in (None, 0) else WorkSpec.ST_failed
            elif batchStatus in ["canceled"]:
                newStatus = WorkSpec.ST_cancelled
            else:
                newStatus = WorkSpec.ST_failed
            tmpLog.debug(f"batchStatus {batchStatus} -> workerStatus {newStatus}")

            if self.iri_debug:
                tmpLog.debug(f"IRI job {job_id} status: {batchStatus}, exitCode: {exitCode}, mapped to workerStatus: {newStatus}")
                tmpLog.debug(f"IRI job {job_id} download stdout/stderr through {self.download_logs_method}.")

            if newStatus in _TERMINAL_STATUSES and self.download_logs:
                worker_id = str(workSpec.workerID)
                if self.download_logs_method == "globus_https" and self.globus_download_dir:
                    remote_log_dir = os.path.join(self.globus_download_dir, worker_id)
                elif not self.remote_log_dir:
                    remote_log_dir = os.path.join(self.remote_work_dir, worker_id)
                else:
                    remote_log_dir = os.path.join(self.remote_log_dir, worker_id)

                stdout_path, stderr_path = _get_stdio_paths_from_job(job)
                if self.download_logs_method == "globus_https" and self.globus_download_dir:
                    stdout_path = _strip_globus_prefix(stdout_path, self.globus_download_dir)
                    stderr_path = _strip_globus_prefix(stderr_path, self.globus_download_dir)
                remote_paths = {
                    f"{worker_id}_stdout.txt": stdout_path or os.path.join(remote_log_dir, f"{worker_id}_stdout.txt"),
                    f"{worker_id}_stderr.txt": stderr_path or os.path.join(remote_log_dir, f"{worker_id}_stderr.txt"),
                }

                # local destinations recorded by the submitter, falling back to logDir
                work_attrs = workSpec.workAttributes or {}
                local_paths = {
                    f"{worker_id}_stdout.txt": work_attrs.get("local_log_stdout"),
                    f"{worker_id}_stderr.txt": work_attrs.get("local_log_stderr"),
                }

                for filename, remote_file_path in remote_paths.items():
                    local_dest = local_paths.get(filename) or os.path.join(self.logDir, filename)
                    if os.path.exists(local_dest):
                        continue
                    os.makedirs(os.path.dirname(local_dest), exist_ok=True)

                    if self.download_logs_method == "globus_https":
                        try:
                            self.globus_client.download(remote_file_path, local_dest)
                            tmpLog.debug(f"downloaded {filename} via Globus HTTPS from {remote_file_path} to {local_dest}")
                        except (GlobusClientError, OSError) as e:
                            tmpLog.error(f"failed to download {filename} via Globus HTTPS from {remote_file_path} to {local_dest}: {e}")
                    else:
                        remote_url = f"{self.remote_export_path.rstrip('/')}/{worker_id}/{filename}"
                        try:
                            self.iri_client.download_from_http(remote_url, local_dest, username=self.htaccess_username, password=self.htaccess_password)
                            tmpLog.debug(f"downloaded {filename} from {remote_url} to {local_dest}")
                        except (IriClientError, OSError) as e:
                            tmpLog.error(f"failed to download {filename} from {remote_url} to {local_dest}: {e}")

            retList.append((newStatus, ""))
        return True, retList
