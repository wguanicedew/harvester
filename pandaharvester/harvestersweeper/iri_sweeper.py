import os
import shutil

from pandaharvester.harvestercore import core_utils
from pandaharvester.harvestermisc.iri_utils import IriClient, IriClientError
from pandaharvester.harvestersweeper.base_sweeper import BaseSweeper

baseLogger = core_utils.setup_logger("iri_sweeper")


class IriSweeper(BaseSweeper):
    def __init__(self, **kwargs):
        BaseSweeper.__init__(self, **kwargs)
        self.iri_config = kwargs.get("iri_config")
        self.iri_resource_id = kwargs.get("iri_resource_id")
        self.iri_debug = kwargs.get("iri_debug", False)
        self.iri_client = IriClient(config_path=self.iri_config, resource_id=self.iri_resource_id, debug=self.iri_debug)

    def kill_worker(self, workspec):
        tmpLog = self.make_logger(baseLogger, f"workerID={workspec.workerID}", method_name="kill_worker")
        job_id = workspec.batchID
        if not job_id:
            return False, "no batchID to kill"

        if self.iri_debug:
            tmpLog.debug(f"cancelling IRI job {job_id}")

        try:
            self.iri_client.cancel_job(job_id, resource_id=self.iri_resource_id)
        except IriClientError as e:
            errStr = f"Failed to cancel IRI job {job_id}: {e}"
            tmpLog.error(errStr)
            return False, errStr

        tmpLog.info(f"Succeeded to kill workerID={workspec.workerID} batchID={job_id}")
        return True, ""

    def sweep_worker(self, workspec):
        tmpLog = self.make_logger(baseLogger, f"workerID={workspec.workerID}", method_name="sweep_worker")
        ap = workspec.accessPoint
        if ap and os.path.exists(ap):
            try:
                shutil.rmtree(ap)
                tmpLog.info(f"Removed directory {ap}")
            except Exception as e:
                err = f"Failed to remove {ap}: {e}"
                tmpLog.error(err)
                return False, err
        else:
            tmpLog.info("Access point already removed or none provided.")
        return True, ""
