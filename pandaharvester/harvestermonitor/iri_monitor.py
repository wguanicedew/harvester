from pandaharvester.harvestercore import core_utils
from pandaharvester.harvestercore.plugin_base import PluginBase
from pandaharvester.harvestercore.work_spec import WorkSpec
from pandaharvester.harvestermisc.iri_utils import IriClient, IriClientError

# logger
baseLogger = core_utils.setup_logger("iri_monitor")


# monitor for IRI API
class IriMonitor(PluginBase):
    # constructor
    def __init__(self, **kwarg):
        PluginBase.__init__(self, **kwarg)
        self.iri_config = kwarg.get("iri_config")
        self.iri_resource_id = kwarg.get("iri_resource_id")
        self.iri_client = IriClient(config_path=self.iri_config, resource_id=self.iri_resource_id)

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
            retList.append((newStatus, ""))
        return True, retList
