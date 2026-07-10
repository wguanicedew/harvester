import os
import stat
import tempfile
from math import ceil

from pandaharvester.harvesterconfig import harvester_config
from pandaharvester.harvestercore import core_utils
from pandaharvester.harvestercore.plugin_base import PluginBase
from pandaharvester.harvestermisc.iri_utils import IriClient, IriClientError

# logger
baseLogger = core_utils.setup_logger("iri_submitter")


# submitter for IRI API
class IriSubmitter(PluginBase):
    # constructor
    def __init__(self, **kwarg):
        self.uploadLog = False
        self.logBaseURL = None
        PluginBase.__init__(self, **kwarg)
        self.iri_config = kwarg.get("iri_config")
        self.iri_resource_id = kwarg.get("iri_resource_id")

        self.pandaTokenFilename = getattr(self, "pandaTokenFilename", None)
        self.pandaTokenDir = getattr(self, "pandaTokenDir", None)
        self.x509_proxy = getattr(self, "x509_proxy", None)
        
        self.remote_executable = kwarg.get("remote_executable", None)
        if not self.remote_executable:
            raise ValueError("remote_executable must be specified in iri_submitter configuration")
        self.remote_work_dir = kwarg.get("remote_work_dir", None)
        if not self.remote_work_dir:
            raise ValueError("remote_work_dir must be specified in iri_submitter configuration")
        self.remote_export_path = kwarg.get("remote_export_path", None)
        self.htaccess_password = None
        if not self.remote_export_path:
            self.download_transfer_output_through_iri = True
        else:
            self.download_transfer_output_through_iri = False
            htaccess_password_file = kwarg.get("htaccess_password", None)
            if htaccess_password_file:
                with open(htaccess_password_file, "r") as f:
                    self.htaccess_password = f.read().strip()

        self.iri_client = IriClient(config_path=self.iri_config,
                                    resource_id=self.iri_resource_id)

        if not hasattr(self, "localQueueName"):
            self.localQueueName = "debug"
        # ncore factor
        try:
            if hasattr(self, "nCoreFactor"):
                if type(self.nCoreFactor) in [dict]:
                    # self.nCoreFactor is a dict for ucore
                    pass
                else:
                    self.nCoreFactor = int(self.nCoreFactor)
                    if (not self.nCoreFactor) or (self.nCoreFactor < 1):
                        self.nCoreFactor = 1
            else:
                self.nCoreFactor = 1
        except AttributeError:
            self.nCoreFactor = 1

    # submit workers
    def submit_workers(self, workspec_list):
        retList = []
        for workSpec in workspec_list:
            # make logger
            tmpLog = self.make_logger(baseLogger, f"workerID={workSpec.workerID}", method_name="submit_workers")
            # set nCore
            if self.nCore > 0:
                workSpec.nCore = self.nCore
            # make batch script, here we create batch script at where harvester install
            batchFile = self.make_batch_script(workSpec, tmpLog)
            placeholder = self.make_placeholder_map(workSpec, tmpLog)
            remote_worker_dir = os.path.join(self.remote_work_dir, workSpec.workerID)

            job_spec = {
                "executable": self.remote_executable,
                "arguments": [batchFile],
                "directory": remote_worker_dir,
                "name": f"harvester-{harvester_config.master.harvester_id}-{workSpec.workerID}",
                "inherit_environment": True,
                "stdout_path": os.path.join(remote_worker_dir, "stdout.txt"),
                "stderr_path": os.path.join(remote_worker_dir, "stderr.txt"),
                "resources": {
                    "node_count": placeholder["nNode"],
                    "process_count": placeholder["nNode"],
                    "processes_per_node": 1,
                    "cpu_cores_per_process": placeholder["nCorePerNode"],
                    "memory": int(placeholder["requestRamBytes"]) if placeholder["requestRamBytes"] else None,
                },
                "attributes": {
                    "duration": int(placeholder["requestWalltime"]) if placeholder["requestWalltime"] else None,
                    "queue_name": self.localQueueName,
                    "account": getattr(self, "project", None),
                },
                "launcher": "single",
            }

            try:
                if self.pandaTokenDir is not None and self.pandaTokenFilename is not None:
                    token_file = os.path.join(self.pandaTokenDir, self.pandaTokenFilename)
                else:
                    token_file = None
                input_files = [batchFile, token_file, self.x509_proxy, os.path.join(workSpec.accessPoint, "pandaJobData.out")]
                archive_file = self.iri_client.create_input_archive(workSpec.accessPoint, input_files)
                remote_archive_path = os.path.join(remote_worker_dir, os.path.basename(archive_file))
                self.iri_client.upload(archive_file, remote_archive_path, resource_id=self.iri_resource_id)
                job = self.iri_client.launch_job(job_spec, resource_id=self.iri_resource_id)
            except IriClientError as e:
                err = f"IRI job submission failed: {e}"
                tmpLog.error(err)
                retList.append((False, err))
                continue

            job_id = job.get("id")
            if not job_id:
                err = f"IRI job submission returned no id: {job}"
                tmpLog.error(err)
                retList.append((False, err))
                continue

            tmpLog.debug(f"Assigned batchID: {job_id}")
            workSpec.batchID = job_id
            retList.append((True, ""))

        return retList

    def get_core_factor(self, workspec, logger):
        try:
            if type(self.nCoreFactor) in [dict]:
                n_core_factor = self.nCoreFactor.get(workspec.jobType, {}).get(workspec.resourceType, 1)
                return int(n_core_factor)
            return int(self.nCoreFactor)
        except Exception as ex:
            logger.warning(f"Failed to get core factor: {ex}")
        return 1

    def make_placeholder_map(self, workspec, logger):
        timeNow = core_utils.naive_utcnow()

        panda_queue_name = self.queueName
        this_panda_queue_dict = dict()

        # get default information from queue info
        n_core_per_node_from_queue = this_panda_queue_dict.get("corecount", 1) if this_panda_queue_dict.get("corecount", 1) else 1

        # get override requirements from queue configured
        try:
            n_core_per_node = self.nCorePerNode if self.nCorePerNode else n_core_per_node_from_queue
        except AttributeError:
            n_core_per_node = n_core_per_node_from_queue
        if not n_core_per_node:
            n_core_per_node = self.nCore

        n_core_factor = self.get_core_factor(workspec, logger)

        n_core_total = workspec.nCore if workspec.nCore else n_core_per_node
        n_core_total_factor = n_core_total * n_core_factor
        request_ram = max(workspec.minRamCount, 1 * n_core_total) if workspec.minRamCount else 1 * n_core_total
        request_disk = workspec.maxDiskCount * 1024 if workspec.maxDiskCount else 1
        request_walltime = workspec.maxWalltime if workspec.maxWalltime else 0

        n_node = ceil(n_core_total / n_core_per_node)
        request_ram_factor = request_ram * n_core_factor
        request_ram_bytes = request_ram * (2**20)
        request_ram_bytes_factor = request_ram_bytes * n_core_factor
        request_ram_per_core = ceil(request_ram * n_node / n_core_total)
        request_ram_bytes_per_core = ceil(request_ram_bytes * n_node / n_core_total)
        request_cputime = request_walltime * n_core_total
        request_walltime_minute = ceil(request_walltime / 60)
        request_cputime_minute = ceil(request_cputime / 60)

        placeholder_map = {
            "nCorePerNode": n_core_per_node,
            "nCoreTotal": n_core_total_factor,
            "nCoreFactor": n_core_factor,
            "nNode": n_node,
            "requestRam": request_ram_factor,
            "requestRamBytes": request_ram_bytes_factor,
            "requestRamPerCore": request_ram_per_core,
            "requestRamBytesPerCore": request_ram_bytes_per_core,
            "requestDisk": request_disk,
            "requestWalltime": request_walltime,
            "requestWalltimeMinute": request_walltime_minute,
            "requestCputime": request_cputime,
            "requestCputimeMinute": request_cputime_minute,
            "accessPoint": workspec.accessPoint,
            "harvesterID": harvester_config.master.harvester_id,
            "workerID": workspec.workerID,
            "computingSite": workspec.computingSite,
            "pandaQueueName": panda_queue_name,
            "localQueueName": self.localQueueName,
            "logDir": self.logDir,
            "logSubDir": os.path.join(self.logDir, timeNow.strftime("%y-%m-%d_%H")),
            "jobType": workspec.jobType,
        }
        for k in ["tokenDir", "tokenName", "tokenOrigin", "submitMode"]:
            try:
                placeholder_map[k] = getattr(self, k)
            except Exception:
                pass
        return placeholder_map

    # make batch script
    def make_batch_script(self, workspec, logger):
        # template for batch script
        with open(self.templateFile) as f:
            template = f.read()
        tmpFile = tempfile.NamedTemporaryFile(delete=False, suffix="_submit.sh", dir=workspec.get_access_point())
        placeholder = self.make_placeholder_map(workspec, logger)
        tmpFile.write(str(template.format_map(core_utils.SafeDict(placeholder))).encode("latin_1"))
        tmpFile.close()

        # set execution bit and group permissions on the temp file
        st = os.stat(tmpFile.name)
        os.chmod(tmpFile.name, st.st_mode | stat.S_IEXEC | stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH)

        return tmpFile.name
