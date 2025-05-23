import os
import shutil
import subprocess

from pandaharvester.harvestercore import core_utils
from pandaharvester.harvestermisc.htcondor_utils import (
    CondorJobManage,
    _runShell,
    condor_job_id_from_workspec,
    get_host_batchid_map,
)
from pandaharvester.harvestersweeper.base_sweeper import BaseSweeper

# Logger
baseLogger = core_utils.setup_logger("htcondor_sweeper")


# sweeper for HTCONDOR batch system
class HTCondorSweeper(BaseSweeper):
    # constructor
    def __init__(self, **kwarg):
        BaseSweeper.__init__(self, **kwarg)

    # # kill a worker
    # def kill_worker(self, workspec):
    #     # Make logger
    #     tmpLog = self.make_logger(baseLogger, 'workerID={0}'.format(workspec.workerID),
    #                               method_name='kill_worker')
    #
    #     # Skip batch operation for workers without batchID
    #     if workspec.batchID is None:
    #         tmpLog.info('Found workerID={0} has submissionHost={1} batchID={2} . Cannot kill. Skipped '.format(
    #                         workspec.workerID, workspec.submissionHost, workspec.batchID))
    #         return True, ''
    #
    #     # Parse condor remote options
    #     name_opt, pool_opt = '', ''
    #     if workspec.submissionHost is None or workspec.submissionHost == 'LOCAL':
    #         pass
    #     else:
    #         try:
    #             condor_schedd, condor_pool = workspec.submissionHost.split(',')[0:2]
    #         except ValueError:
    #             errStr = 'Invalid submissionHost: {0} . Skipped'.format(workspec.submissionHost)
    #             tmpLog.error(errStr)
    #             return False, errStr
    #         name_opt = '-name {0}'.format(condor_schedd) if condor_schedd else ''
    #         pool_opt = '-pool {0}'.format(condor_pool) if condor_pool else ''
    #
    #     # Kill command
    #     comStr = 'condor_rm {name_opt} {pool_opt} {batchID}'.format(name_opt=name_opt,
    #                                                                 pool_opt=pool_opt,
    #                                                                 batchID=workspec.batchID)
    #     (retCode, stdOut, stdErr) = _runShell(comStr)
    #     if retCode != 0:
    #         comStr = 'condor_q -l {name_opt} {pool_opt} {batchID}'.format(name_opt=name_opt,
    #                                                                     pool_opt=pool_opt,
    #                                                                     batchID=workspec.batchID)
    #         (retCode, stdOut, stdErr) = _runShell(comStr)
    #         if ('ClusterId = {0}'.format(workspec.batchID) in str(stdOut) \
    #             and 'JobStatus = 3' not in str(stdOut)) or retCode != 0:
    #             # Force to cancel if batch job not terminated first time
    #             comStr = 'condor_rm -forcex {name_opt} {pool_opt} {batchID}'.format(name_opt=name_opt,
    #                                                                         pool_opt=pool_opt,
    #                                                                         batchID=workspec.batchID)
    #             (retCode, stdOut, stdErr) = _runShell(comStr)
    #             if retCode != 0:
    #                 # Command failed to kill
    #                 errStr = 'command "{0}" failed, retCode={1}, error: {2} {3}'.format(comStr, retCode, stdOut, stdErr)
    #                 tmpLog.error(errStr)
    #                 return False, errStr
    #         # Found already killed
    #         tmpLog.info('Found workerID={0} submissionHost={1} batchID={2} already killed'.format(
    #                         workspec.workerID, workspec.submissionHost, workspec.batchID))
    #     else:
    #         tmpLog.info('Succeeded to kill workerID={0} submissionHost={1} batchID={2}'.format(
    #                         workspec.workerID, workspec.submissionHost, workspec.batchID))
    #     # Return
    #     return True, ''

    # kill workers

    def kill_workers(self, workspec_list):
        # Make logger
        tmpLog = self.make_logger(baseLogger, method_name="kill_workers")
        tmpLog.debug("start")
        # Initialization
        all_job_ret_map = {}
        retList = []
        # Kill
        for submissionHost, batchIDs_dict in get_host_batchid_map(workspec_list).items():
            batchIDs_list = list(batchIDs_dict.keys())
            try:
                condor_job_manage = CondorJobManage(id=submissionHost)
                ret_map = condor_job_manage.remove(batchIDs_list)
            except Exception as e:
                ret_map = {}
                ret_err_str = f"Exception {e.__class__.__name__}: {e}"
                tmpLog.error(ret_err_str)
            all_job_ret_map.update(ret_map)
        # Fill return list
        for workspec in workspec_list:
            if workspec.batchID is None:
                ret = (True, "worker without batchID; skipped")
            else:
                ret = all_job_ret_map.get(condor_job_id_from_workspec(workspec), (False, "batch job not found in return map"))
            retList.append(ret)
        tmpLog.debug("done")
        # Return
        return retList

    # cleanup for a worker
    def sweep_worker(self, workspec):
        # Make logger
        tmpLog = self.make_logger(baseLogger, f"workerID={workspec.workerID}", method_name="sweep_worker")
        tmpLog.debug("start")
        # Clean up preparator base directory (staged-in files)
        try:
            preparatorBasePath = self.preparatorBasePath
        except AttributeError:
            tmpLog.debug("No preparator base directory is configured. Skipped cleaning up preparator directory")
        else:
            if os.path.isdir(preparatorBasePath):
                if not workspec.get_jobspec_list():
                    tmpLog.warning(f"No job PandaID found relate to workerID={workspec.workerID}. Skipped cleaning up preparator directory")
                else:
                    for jobspec in workspec.get_jobspec_list():
                        preparator_dir_for_cleanup = os.path.join(preparatorBasePath, str(jobspec.PandaID))
                        if os.path.isdir(preparator_dir_for_cleanup) and preparator_dir_for_cleanup != preparatorBasePath:
                            try:
                                shutil.rmtree(preparator_dir_for_cleanup)
                            except OSError as _err:
                                if "No such file or directory" in _err.strerror:
                                    tmpLog.debug(f"Found that {_err.filename} was already removed")
                                pass
                            tmpLog.info(f"Succeeded to clean up preparator directory: Removed {preparator_dir_for_cleanup}")
                        else:
                            errStr = f"Failed to clean up preparator directory: {preparator_dir_for_cleanup} does not exist or invalid to be cleaned up"
                            tmpLog.error(errStr)
                            return False, errStr
            else:
                errStr = f"Configuration error: Preparator base directory {preparatorBasePath} does not exist"
                tmpLog.error(errStr)
                return False, errStr
        tmpLog.info(f"Succeeded to clean up everything about workerID={workspec.workerID}")
        tmpLog.debug("done")
        # Return
        return True, ""
