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
        self.pandaTokenKeyFilename = getattr(self, "pandaTokenKeyFilename", None)
        self.pandaAuthOrigin = getattr(self, "pandaAuthOrigin", None)
        self.x509UserProxy = getattr(self, "x509UserProxy", os.getenv("X509_USER_PROXY"))

        self.templateFile = kwarg.get("templateFile", None)
        self.remoteQueueName = kwarg.get("remoteQueueName", None)
        self.duration = kwarg.get("duration", None)
        
        self.remote_executable = kwarg.get("remote_executable", None)
        if not self.remote_executable:
            raise ValueError("remote_executable must be specified in iri_submitter configuration")
        self.remote_work_dir = kwarg.get("remote_work_dir", None)
        if not self.remote_work_dir:
            raise ValueError("remote_work_dir must be specified in iri_submitter configuration")
        self.remote_log_dir = kwarg.get("remote_log_dir", None)
        if not self.remote_log_dir:
            raise ValueError("remote_log_dir must be specified in iri_submitter configuration")
        self.remote_export_path = kwarg.get("remote_export_path", None)
        self.remote_input_cache = kwarg.get("remote_input_cache", None)
        # IRI rejects gpu_cores_per_process < 1, so omit it from job_spec unless set
        self.gpu_cores_per_process = int(kwarg.get("gpu_cores_per_process", 0))
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
            placeholder = self.make_placeholder_map(workSpec, tmpLog)
            batchFile = self.make_batch_script(workSpec, placeholder, tmpLog)
            remote_worker_dir = os.path.join(self.remote_work_dir, str(workSpec.workerID))
            if self.duration:
                duration = self.duration
            else:
                duration = int(placeholder["requestWalltime"]) if placeholder["requestWalltime"] else None

            # Execution flow on the remote resource (see examples/hpc/nersc/perlmutter_iri_main.sh
            # and examples/hpc/nersc/perlmutter_iri_submit_template.sh for a concrete example):
            #   1) build a tar archive here containing executable_batch (the batch script rendered
            #      from templateFile), pandaTokenFilename, pandaTokenKeyFilename, x509UserProxy and
            #      pandaJobData.out
            #   2) upload the archive to remote_input_cache on the remote resource
            #   3) launch remote_executable (pre-deployed on the remote resource) with its cwd set to
            #      remote_worker_dir (job_spec["directory"]), passing --input_archive <remote_archive_path>
            #   4) remote_executable copies the archive into that working directory and untars it there
            #   5) remote_executable runs the extracted "executable_batch" script from that same
            #      directory, so "$(pwd)/<name>" inside the batch script resolves to the other
            #      extracted files (pandaTokenFilename, pandaTokenKeyFilename, x509UserProxy)
            try:
                if self.pandaTokenDir is not None and self.pandaTokenFilename is not None:
                    token_file = os.path.join(self.pandaTokenDir, self.pandaTokenFilename)
                else:
                    token_file = None
                input_maps = {"executable_batch": batchFile,
                              "pandaTokenFilename": token_file,
                              "pandaTokenKeyFilename": self.pandaTokenKeyFilename,
                              "x509UserProxy": self.x509UserProxy,
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
            
            remote_log_dir = placeholder["remote_log_dir"]
            remote_worker_dir = placeholder["work_dir"]
            stdout_path = placeholder["stdout_path"]
            stderr_path = placeholder["stderr_path"]

            # remote_executable (e.g. examples/hpc/nersc/perlmutter_iri_main.sh) expects:
            #   --input_archive <input_archive> --work_dir <work_dir> --log_dir <log_dir>
            #   --batch_executable <batch_executable>
            submit_args = (
                f"--input_archive {remote_archive_path} --work_dir {remote_worker_dir} --log_dir {remote_log_dir} "
                "--batch_executable executable_batch"
            ).split()

            job_spec = {
                "executable": self.remote_executable,
                "arguments": submit_args,
                "directory": remote_worker_dir,
                "name": f"{harvester_config.master.harvester_id}-{workSpec.workerID}",
                "inherit_environment": True,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "resources": {
                    "node_count": placeholder["nNode"],
                    "process_count": placeholder["nNode"] * placeholder["nProcessPerNode"],
                    "processes_per_node": placeholder["nProcessPerNode"],
                    "cpu_cores_per_process": placeholder["nCorePerProcess"],
                    "exclusive_node_use": True,
                    "memory": int(placeholder["requestRamBytes"]) * placeholder["nCorePerNode"] * placeholder["nNode"] if placeholder["requestRamBytes"] else None,
                    "additionalProp1": {},
                },
                "attributes": {
                    "duration": int(duration) if duration else None,
                    "queue_name": self.remoteQueueName,
                    "account": getattr(self, "project", None),
                    "reservation_id": getattr(self, "reservation_id", None),
                    "additionalProp1": {}
                },
                "pre_launch": getattr(self, "pre_launch", None),   # model load cvmfs
                "post_launch": getattr(self, "post_launch", None),
                "launcher": "single",  # single, mpirun, srun, aprun, jsrun
            }
            if self.gpu_cores_per_process >= 1:
                job_spec["resources"]["gpu_cores_per_process"] = self.gpu_cores_per_process
            custom_attributes = {}
            if getattr(self, "constraint", None) is not None:
                custom_attributes["constraint"] = self.constraint
            if getattr(self, "signal", None) is not None:
                custom_attributes["signal"] = self.signal
            if getattr(self, "no_requeue", None):
                custom_attributes["no_requeue"] = True
            if getattr(self, "licenses", None) is not None:
                custom_attributes["licenses"] = self.licenses
            if getattr(self, "export", None) is not None:
                custom_attributes["export"] = self.export
            if getattr(self, "image", None) is not None:
                custom_attributes["image"] = self.image
            if getattr(self, "volume", None) is not None:
                custom_attributes["volume"] = self.volume
            if custom_attributes:
                job_spec["attributes"]["custom_attributes"] = custom_attributes
            placeholder_custom_attributes = placeholder.get("custom_attributes", {})
            if placeholder_custom_attributes:
                job_spec["attributes"]["custom_attributes"] = {**job_spec["attributes"].get("custom_attributes", {}), **placeholder_custom_attributes}

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
            nCorePerNode = getattr(self, "nCorePerNode", None)
            n_core_per_node = nCorePerNode if nCorePerNode else n_core_per_node_from_queue
        except AttributeError:
            n_core_per_node = n_core_per_node_from_queue
        if not n_core_per_node:
            n_core_per_node = self.nCore

        n_core_factor = self.get_core_factor(workspec, logger)

        n_process_per_node = getattr(self, "nProcessPerNode", 1)
        n_core_per_process = n_core_per_node / n_process_per_node

        n_node = getattr(self, "nNode", 1)

        n_core_total = n_core_per_node * n_node
        n_core_total_factor = n_core_total * n_core_factor
        request_ram = max(workspec.minRamCount, 1 * n_core_total) if workspec.minRamCount else 1 * n_core_total
        request_disk = workspec.maxDiskCount * 1024 if workspec.maxDiskCount else 1
        request_walltime = workspec.maxWalltime if workspec.maxWalltime else 0

        request_ram_factor = request_ram * n_core_factor
        request_ram_bytes = request_ram * (2**20)
        request_ram_bytes_factor = request_ram_bytes * n_core_factor
        request_ram_per_core = request_ram

        request_ram_bytes_per_core = request_ram * (2**20)
        request_ram_bytes_factor_per_core = request_ram_bytes_per_core * n_core_factor

        request_ram_total = request_ram * n_core_total
        request_ram_total_factor = request_ram_total * n_core_factor
        request_ram_bytes_total = request_ram_bytes_per_core * n_core_total
        request_ram_bytes_total_factor = request_ram_bytes_total * n_core_factor

        request_cputime = request_walltime * n_core_total
        request_walltime_minute = ceil(request_walltime / 60)
        request_cputime_minute = ceil(request_cputime / 60)

        log_sub_dir = os.path.join(self.logDir, timeNow.strftime("%y-%m-%d_%H"))
        if self.logBaseURL and self.logDir:
            rel_stdout = os.path.relpath(os.path.join(log_sub_dir, f"{workspec.workerID}_stdout.txt"), self.logDir)
            gtag = os.path.join(self.logBaseURL, rel_stdout)
        else:
            gtag = "unknown"

        # The names below must match the arcname keys used to build the input archive in
        # submit_workers, since that's the filename each ends up with after being untarred
        # into work_dir on the remote resource (see the "Execution flow" comment there).
        has_panda_token = bool(self.pandaTokenDir) and bool(self.pandaTokenFilename)
        has_panda_token_key = bool(self.pandaTokenKeyFilename)
        has_x509_proxy = bool(self.x509UserProxy)
        remote_log_dir = os.path.join(self.remote_log_dir, str(workspec.workerID))

        placeholder_map = {
            "nCorePerNode": n_core_per_node,
            "nCorePerProcess": n_core_per_process,
            "nProcessPerNode": n_process_per_node,
            "nCoreTotal": n_core_total_factor,
            "nCoreFactor": n_core_factor,
            "nNode": n_node,
            "requestRam": request_ram_total_factor,
            "requestRamBytes": request_ram_bytes_total_factor,
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
            "logSubDir": log_sub_dir,
            "gtag": gtag,
            "jobType": workspec.jobType,
            "prodSourceLabel": workspec.jobType,
            "pilotType": workspec.pilotType,
            "work_dir": os.path.join(self.remote_work_dir, str(workspec.workerID)),
            "remote_log_dir": remote_log_dir,
            "pandaTokenFilename": "pandaTokenFilename" if has_panda_token else "",
            "pandaTokenKeyFilename": "pandaTokenKeyFilename" if has_panda_token_key else "",
            "x509UserProxy": "x509UserProxy" if has_x509_proxy else "",
            "stdout_path": os.path.join(remote_log_dir, f"{workspec.workerID}_stdout.txt"),
            "stderr_path": os.path.join(remote_log_dir, f"{workspec.workerID}_stderr.txt"),
            "custom_attributes": getattr(self, "custom_attributes", {}),
        }
        for k in ["tokenDir", "tokenName", "tokenOrigin", "submitMode"]:
            try:
                placeholder_map[k] = getattr(self, k)
            except Exception:
                pass
        return placeholder_map

    # make batch script
    def make_batch_script(self, workspec, placeholder, logger):
        # template for batch script
        with open(self.templateFile) as f:
            template = f.read()
        tmpFile = tempfile.NamedTemporaryFile(delete=False, suffix="_submit.sh", dir=workspec.get_access_point())
        tmpFile.write(str(template.format_map(core_utils.SafeDict(placeholder))).encode("latin_1"))
        tmpFile.close()
        if self.iri_debug:
            logger.debug(f"Rendered batch script {tmpFile.name} from template {self.templateFile}")

        # set execution bit and group permissions on the temp file
        st = os.stat(tmpFile.name)
        os.chmod(tmpFile.name, st.st_mode | stat.S_IEXEC | stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH)

        return tmpFile.name
