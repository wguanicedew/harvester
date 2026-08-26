import os

from pandaharvester.harvestercore import core_utils
from pandaharvester.harvestercore.plugin_base import PluginBase
from pandaharvester.harvestercore.work_spec import WorkSpec
from pandaharvester.harvestermisc.iri_utils import GlobusClient, GlobusClientError, IriClient, IriClientError

# logger
baseLogger = core_utils.setup_logger("iri_monitor")

# statuses for which the remote job has stopped running and its output, if any, is ready
_TERMINAL_STATUSES = (WorkSpec.ST_finished, WorkSpec.ST_failed, WorkSpec.ST_cancelled)


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
        self.remote_export_path = kwarg.get("remote_export_path", None)
        self.download_logs = kwarg.get("download_logs", False)
        self.download_logs_method = kwarg.get("download_logs_method", "globus_https")
        self.globus_https_config = kwarg.get("globus_https_config", None)
        self.globus_client = None
        if self.download_logs and self.download_logs_method == "globus_https":
            self.globus_client = GlobusClient(config_path=self.globus_https_config, debug=self.iri_debug)

        self.htaccess_username = kwarg.get("htaccess_username", None)
        htaccess_password_file = kwarg.get("htaccess_password", None)
        if htaccess_password_file:
            with open(htaccess_password_file) as f:
                self.htaccess_password = f.read().strip()
        else:
            self.htaccess_password = None

        self.logDir = kwarg.get("logDir", None)

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
                tmpLog.debug(f"IRI job {job_id} download stdout/stderr through Gloubus HTTPS.")

            if newStatus in _TERMINAL_STATUSES and self.download_logs:
                if not self.remote_log_dir:
                    remote_log_dir = os.path.join(self.remote_work_dir, workSpec.workerID)
                else:
                    remote_log_dir = os.path.join(self.remote_log_dir, workSpec.workerID)

                for filename in (f"{workSpec.workerID}_stdout.txt", f"{workSpec.workerID}_{workSpec.workerID}_stderr.txt"):
                    remote_file_path = os.path.join(remote_log_dir, filename)
                    local_dest = os.path.join(self.logDir, filename)
                    if os.path.exists(local_dest):
                        continue

                    if self.download_logs_method == "globus_https":
                        try:
                            self.globus_client.download(remote_file_path, local_dest)
                            tmpLog.debug(f"downloaded {filename} via Globus HTTPS from {remote_file_path} to {local_dest}")
                        except GlobusClientError as e:
                            tmpLog.error(f"failed to download {filename} via Globus HTTPS from {remote_file_path}: {e}")
                    else:
                        remote_url = f"{self.remote_export_path.rstrip('/')}/{workSpec.workerID}/{filename}"
                        try:
                            self.iri_client.download_from_http(remote_url, local_dest, username=self.htaccess_username, password=self.htaccess_password)
                            tmpLog.debug(f"downloaded {filename} from {remote_url} to {local_dest}")
                        except IriClientError as e:
                            tmpLog.error(f"failed to download {filename} from {remote_url}: {e}")

            retList.append((newStatus, ""))
        return True, retList
