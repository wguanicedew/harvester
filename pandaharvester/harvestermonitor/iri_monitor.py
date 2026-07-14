import os

from pandaharvester.harvestercore import core_utils
from pandaharvester.harvestercore.plugin_base import PluginBase
from pandaharvester.harvestercore.work_spec import WorkSpec
from pandaharvester.harvestermisc.iri_utils import IriClient, IriClientError

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

        self.remote_export_path = kwarg.get("remote_export_path", None)
        self.download_transfer_output_through_iri = not bool(self.remote_export_path)
        self.htaccess_username = kwarg.get("htaccess_username", None)
        htaccess_password_file = kwarg.get("htaccess_password", None)
        if htaccess_password_file:
            with open(htaccess_password_file) as f:
                self.htaccess_password = f.read().strip()
        else:
            self.htaccess_password = None

    def check_workers(self, workspec_list):
        retList = []
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

            if newStatus in _TERMINAL_STATUSES and not self.download_transfer_output_through_iri:
                for filename in ("stdout.txt", "stderr.txt"):
                    local_dest = os.path.join(workSpec.accessPoint, filename)
                    if os.path.exists(local_dest):
                        continue
                    remote_url = f"{self.remote_export_path.rstrip('/')}/{workSpec.workerID}/{filename}"
                    try:
                        self.iri_client.download_from_http(remote_url, local_dest, username=self.htaccess_username, password=self.htaccess_password)
                        tmpLog.debug(f"downloaded {filename} from {remote_url} to {local_dest}")
                    except IriClientError as e:
                        tmpLog.error(f"failed to download {filename} from {remote_url}: {e}")

            retList.append((newStatus, ""))
        return True, retList
