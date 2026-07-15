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
        self.iri_debug = kwarg.get("iri_debug", False)

        self.pandaTokenFilename = getattr(self, "pandaTokenFilename", None)
        self.pandaTokenDir = getattr(self, "pandaTokenDir", None)
        self.x509_proxy = getattr(self, "x509_proxy", None)

        self.templateFile = kwarg.get("templateFile", None)
        self.remoteQueueName = kwarg.get("remoteQueueName", None)
        self.duration = kwarg.get("duration", None)
        
        self.remote_executable = kwarg.get("remote_executable", None)
        if not self.remote_executable:
            raise ValueError("remote_executable must be specified in iri_submitter configuration")
        self.remote_work_dir = kwarg.get("remote_work_dir", None)
        if not self.remote_work_dir:
            raise ValueError("remote_work_dir must be specified in iri_submitter configuration")
        self.remote_export_path = kwarg.get("remote_export_path", None)
        self.remote_input_cache = kwarg.get("remote_input_cache", None)
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
                                    resource_id=self.iri_resource_id,
                                    debug=self.iri_debug)

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
            remote_worker_dir = os.path.join(self.remote_work_dir, str(workSpec.workerID))
            if self.duration:
                duration = self.duration
            else:
                duration = int(placeholder["requestWalltime"]) if placeholder["requestWalltime"] else None

            try:
                if self.pandaTokenDir is not None and self.pandaTokenFilename is not None:
                    token_file = os.path.join(self.pandaTokenDir, self.pandaTokenFilename)
                    token_vo = placeholder.get("tokenOrigin", None)
                    token_vo_file = None
                    if token_vo:
                        token_vo_file = os.path.join(self.pandaTokenDir, f"token_vo")
                        with open(token_vo_file, "w") as f:
                            f.write(token_vo)
                else:
                    token_file = None
                    token_vo_file = None
                input_maps = {"executable_batch": batchFile,
                              "token_file": token_file,
                              "token_vo_file": token_vo_file,
                              "x509_proxy": self.x509_proxy,
                              "pandaJobData.out": os.path.join(workSpec.accessPoint, "pandaJobData.out")}
                archive_file = self.iri_client.create_input_archive(workSpec.accessPoint, input_maps)
                if self.iri_debug:
                    tmpLog.debug(f"Created input archive: {archive_file}")
                if self.remote_input_cache:
                    remote_input_cache = self.remote_input_cache
                else:
                    remote_input_cache = os.path.join(self.remote_work_dir, "input_cache")
                archive_name = os.path.basename(archive_file)
                remote_archive_name = f"{workSpec.workerID}_{archive_name}"
                remote_archive_path = os.path.join(remote_input_cache, remote_archive_name)
                ret = self.iri_client.upload(archive_file, remote_archive_path, resource_id=self.iri_resource_id)
                if self.iri_debug:
                    tmpLog.debug(f"Uploaded input archive {archive_file} to {remote_archive_path}: {ret}")
            except IriClientError as e:
                err = f"IRI upload inputs failed: {e}"
                tmpLog.error(err)
                retList.append((False, err))
                continue
            
            pilot_args_template = (
                f"--input_archive {remote_archive_path} "
                "--ntasks-total {nCoreTotal} --ntasks 1 --cpus-per-task 1 --mem-per-cpu {requestRamPerCore} "
                "-s {computingSite} -r {computingSite} -q {pandaQueueName} -j {prodSourceLabel} -i {pilotType} "
                "--es-executor-type fineGrainedProc -w generic --pilot-user epic --allow-same-user false "
                "--url https://pandaserver01.sdcc.bnl.gov -p 25443 --harvester-submit-mode PULL "
                "--queuedata-url https://pandaserver01.sdcc.bnl.gov:25443/cache/schedconfig/{computingSite}.all.json "
                "--use-rucio-traces False --rucio-host https://nprucio01.sdcc.bnl.gov:443 "
            )

            #  -s E1_JLAB -r E1_JLAB -e eic -q E1_JLAB -j unified -i PR -t -w generic
            # --pilot-user epic --url https://pandaserver01.sdcc.bnl.gov -p 25443 -d
            # --harvester-submit-mode PUSH --allow-same-user=False --job-type=test 
            # --resource-type SCORE --pilotversion 3 --use-rucio
            # -traces False --rucio-host https://nprucio01.sdcc.bnl.gov:443
            pilot_args = pilot_args_template.format_map(core_utils.SafeDict(placeholder)).split()

            job_spec = {
                "executable": self.remote_executable,
                "arguments": pilot_args,
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
                    "duration": int(duration) if duration else None,
                    "queue_name": self.remoteQueueName,
                    "account": getattr(self, "project", None),
                },
                "launcher": "single",
            }

            try:
                if self.iri_debug:
                    tmpLog.debug(f"To submit job with job_spec: {job_spec}")
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

            remote_export_path = self.remote_export_path.rstrip("/") if self.remote_export_path else None
            if remote_export_path:
                rel_stdOut = f"{workSpec.workerID}/stdout.txt"
                rel_stdErr = f"{workSpec.workerID}/stderr.txt"
                log_stdOut = os.path.join(remote_export_path, rel_stdOut)
                log_stdErr = os.path.join(remote_export_path, rel_stdErr)
                workSpec.set_log_file("stdout", log_stdOut)
                workSpec.set_log_file("stderr", log_stdErr)

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

        n_core_total = self.nCore if self.nCore else n_core_per_node
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
            "prodSourceLabel": workspec.jobType,
            "pilotType": workspec.pilotType,
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
