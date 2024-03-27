import datetime
import random
import socket

from pandaharvester.harvesterbody.agent_base import AgentBase
from pandaharvester.harvesterconfig import harvester_config
from pandaharvester.harvestercore import core_utils
from pandaharvester.harvestercore.db_proxy_pool import DBProxyPool as DBProxy
from pandaharvester.harvestercore.file_spec import FileSpec
from pandaharvester.harvestercore.job_spec import JobSpec
from pandaharvester.harvestercore.plugin_factory import PluginFactory
from pandaharvester.harvestercore.resource_type_constants import (
    BASIC_RESOURCE_TYPE_MULTI_CORE,
    BASIC_RESOURCE_TYPE_SINGLE_CORE,
)
from pandaharvester.harvestercore.resource_type_mapper import ResourceTypeMapper
from pandaharvester.harvestermisc.info_utils import PandaQueuesDict

# logger
_logger = core_utils.setup_logger("job_fetcher")

# resource type mapper
rt_mapper = ResourceTypeMapper()
all_resource_types = rt_mapper.get_all_resource_types()


# class to fetch jobs
class JobFetcher(AgentBase):
    # constructor
    def __init__(self, communicator, queue_config_mapper, single_mode=False):
        AgentBase.__init__(self, single_mode)
        self.dbProxy = DBProxy()
        self.communicator = communicator
        self.nodeName = socket.gethostname()
        self.queueConfigMapper = queue_config_mapper
        self.pluginFactory = PluginFactory()

    # main loop
    def run(self):
        while True:
            mainLog = self.make_logger(_logger, f"id={self.get_pid()}", method_name="run")
            mainLog.debug("getting number of jobs to be fetched")
            # get number of jobs to be fetched
            nJobsPerQueue = self.dbProxy.get_num_jobs_to_fetch(harvester_config.jobfetcher.nQueues, harvester_config.jobfetcher.lookupTime)
            mainLog.debug(f"got {len(nJobsPerQueue)} queues")
            # get up to date queue configuration
            pandaQueueDict = PandaQueuesDict(filter_site_list=nJobsPerQueue.keys())
            # get job statistics
            job_stats_dict = self.dbProxy.get_job_stats_full()
            if job_stats_dict is None:
                mainLog.warning(f"cannot get job stats")
            # loop over all queues
            for queueName, nJobs in nJobsPerQueue.items():
                # check queue
                if not self.queueConfigMapper.has_queue(queueName):
                    continue
                tmpLog = self.make_logger(_logger, f"queueName={queueName}", method_name="run")
                # get queue
                queueConfig = self.queueConfigMapper.get_queue(queueName)
                siteName = queueConfig.siteName
                # upper limit
                if nJobs > harvester_config.jobfetcher.maxJobs:
                    nJobs = harvester_config.jobfetcher.maxJobs
                if nJobs == 0:
                    tmpLog.debug("no job to fetch; skip")
                    continue
                # get jobs
                try:
                    is_grandly_unified_queue = pandaQueueDict.is_grandly_unified_queue(siteName)
                except Exception:
                    is_grandly_unified_queue = False

                default_prodSourceLabel = queueConfig.get_source_label(is_gu=is_grandly_unified_queue)
                # randomize prodsourcelabel if configured
                pdpm = getattr(queueConfig, "prodSourceLabelRandomWeightsPermille", {})
                choice_list = core_utils.make_choice_list(pdpm=pdpm, default=default_prodSourceLabel)
                prodSourceLabel = random.choice(choice_list)
                # caps from resource_type_limit params on CRIC of the PQ
                resource_type_limits_dict = dict()
                for key, val in pandaQueueDict.get_harvester_params(siteName).items():
                    if str(key).startswith("resource_type_limits."):
                        new_key = str(key).lstrip("resource_type_limits.")
                        if isinstance(val, int):
                            resource_type_limits_dict[new_key] = val
                # FIXME: all parts about HIMEM are temporary as HIMEM rtypes and parameters will be replaced or reimplemented
                # compute cores of active (submitted and running) jobs
                n_jobs_rem = nJobs
                pq_mcore_corecount = pandaQueueDict.get("corecount", 8) or 8
                rt_n_jobs_dict = {}
                rt_n_cores_dict = {
                    "normal": {
                        "starting": 0,
                        "running": 0,
                    },
                    "HIMEM": {
                        "starting": 0,
                        "running": 0,
                    },
                }
                if job_stats_dict and queueName in job_stats_dict:
                    for tmp_rt, val_dict in job_stats_dict[queueName].items():
                        for tmp_status in ["starting", "running"]:
                            increment = val_dict["cores"][tmp_status]
                            if rt_mapper.is_high_memory_resource_type(tmp_rt):
                                rt_n_cores_dict["HIMEM"][tmp_status] += increment
                            else:
                                rt_n_cores_dict["normal"][tmp_status] += increment
                # compute n_jobs to fetch for resource types
                for j, resource_type in enumerate(random.sample(list(all_resource_types), k=len(all_resource_types))):
                    # compute n jobs to get for this resource type
                    rt_n_jobs = n_jobs_rem / (len(all_resource_types) - j)
                    if job_stats_dict and queueName in job_stats_dict:
                        pq_rt_job_stats_dict = job_stats_dict[queueName].get(resource_type, {}).get("jobs", {})
                        rt_n_active_jobs = pq_rt_job_stats_dict.get("starting", 0) + pq_rt_job_stats_dict.get("running", 0)
                        if resource_type in resource_type_limits_dict:
                            # capped by limit of specific resource type
                            rt_n_jobs = min(rt_n_jobs, resource_type_limits_dict[resource_type] - rt_n_active_jobs)
                        if "HIMEM" in resource_type_limits_dict and rt_mapper.is_high_memory_resource_type(resource_type):
                            # capped by total cores of HIMEM
                            rt_n_active_himem_cores = rt_n_cores_dict["HIMEM"]["starting"] + rt_n_cores_dict["HIMEM"]["running"]
                            rt_corecount = 1
                            if not rt_mapper.get_single_core_resource_types(resource_type):
                                rt_corecount = pq_mcore_corecount
                            rt_n_jobs = min(rt_n_jobs, (resource_type_limits_dict["HIMEM"] - rt_n_active_himem_cores) / rt_corecount)
                    rt_n_jobs = max(rt_n_jobs, 0)
                    rt_n_jobs_dict[resource_type] = rt_n_jobs
                    n_jobs_rem -= rt_n_jobs

                # fucntion to call get jobs
                def _get_jobs(resource_type=None, n_jobs=0):
                    # custom criteria from queueconfig
                    additional_criteria = queueConfig.getJobCriteria
                    if resource_type:
                        # addition criteria for getJob on resourcetype
                        additional_criteria = {"resourceType": resource_type}
                    # call get jobs
                    tmpLog.debug(f"getting {n_jobs} jobs for prodSourceLabel={prodSourceLabel} rtype={resource_type}")
                    sw = core_utils.get_stopwatch()
                    if n_jobs > 0:
                        jobs, errStr = self.communicator.get_jobs(siteName, self.nodeName, prodSourceLabel, self.nodeName, n_jobs, additional_criteria)
                    else:
                        jobs, errStr = [], "no need to get job"
                    tmpLog.info(f"got {len(jobs)} jobs for prodSourceLabel={prodSourceLabel} rtype={resource_type} with {errStr} {sw.get_elapsed_time()}")
                    # convert to JobSpec
                    if len(jobs) > 0:
                        # get extractor plugin
                        if hasattr(queueConfig, "extractor"):
                            extractorCore = self.pluginFactory.get_plugin(queueConfig.extractor)
                        else:
                            extractorCore = None
                        jobSpecs = []
                        fileStatMap = dict()
                        sw_startconvert = core_utils.get_stopwatch()
                        for job in jobs:
                            timeNow = core_utils.naive_utcnow()
                            jobSpec = JobSpec()
                            jobSpec.convert_job_json(job)
                            jobSpec.computingSite = queueName
                            jobSpec.status = "starting"
                            jobSpec.subStatus = "fetched"
                            jobSpec.creationTime = timeNow
                            jobSpec.stateChangeTime = timeNow
                            jobSpec.configID = queueConfig.configID
                            jobSpec.set_one_attribute("schedulerID", f"harvester-{harvester_config.master.harvester_id}")
                            if queueConfig.zipPerMB is not None and jobSpec.zipPerMB is None:
                                jobSpec.zipPerMB = queueConfig.zipPerMB
                            fileGroupDictList = [jobSpec.get_input_file_attributes()]
                            if extractorCore is not None:
                                fileGroupDictList.append(extractorCore.get_aux_inputs(jobSpec))
                            for fileGroupDict in fileGroupDictList:
                                for tmpLFN, fileAttrs in fileGroupDict.items():
                                    # make file spec
                                    fileSpec = FileSpec()
                                    fileSpec.PandaID = jobSpec.PandaID
                                    fileSpec.taskID = jobSpec.taskID
                                    fileSpec.lfn = tmpLFN
                                    fileSpec.endpoint = queueConfig.ddmEndpointIn
                                    fileSpec.scope = fileAttrs["scope"]
                                    if "INTERNAL_FileType" in fileAttrs:
                                        fileSpec.fileType = fileAttrs["INTERNAL_FileType"]
                                        jobSpec.auxInput = JobSpec.AUX_hasAuxInput
                                    else:
                                        fileSpec.fileType = "input"
                                    # check file status
                                    if tmpLFN not in fileStatMap:
                                        fileStatMap[tmpLFN] = self.dbProxy.get_file_status(tmpLFN, fileSpec.fileType, queueConfig.ddmEndpointIn, "starting")
                                    # set preparing to skip stage-in if the file is (being) taken care of by another job
                                    if [x for x in ["ready", "preparing", "to_prepare", "triggered"] if x in fileStatMap[tmpLFN]]:
                                        fileSpec.status = "preparing"
                                    else:
                                        fileSpec.status = "to_prepare"
                                    fileStatMap[tmpLFN].setdefault(fileSpec.status, None)
                                    if "INTERNAL_URL" in fileAttrs:
                                        fileSpec.url = fileAttrs["INTERNAL_URL"]
                                    jobSpec.add_in_file(fileSpec)
                            jobSpec.trigger_propagation()
                            jobSpecs.append(jobSpec)
                        # insert to DB
                        tmpLog.debug(f"Converting of {len(jobs)} jobs {sw_startconvert.get_elapsed_time()}")
                        sw_insertdb = core_utils.get_stopwatch()
                        self.dbProxy.insert_jobs(jobSpecs)
                        tmpLog.debug(f"Insert of {len(jobSpecs)} jobs {sw_insertdb.get_elapsed_time()}")
                    # return len jobs
                    return len(jobs)

                # call get jobs
                if all([val > 0 for val in rt_n_jobs_dict.values()]):
                    # no n_jobs limit on any resourcetypes, call get_jobs without constraint
                    _get_jobs(resource_type=None, n_jobs=nJobs)
                else:
                    # call get_jobs for each resourcetype with calculated rt_n_jobs
                    n_jobs_rem = nJobs
                    for resource_type, rt_n_jobs in rt_n_jobs_dict.items():
                        n_jobs_to_get = max(min(round(rt_n_jobs), n_jobs_rem), 0)
                        got_n_jobs = _get_jobs(resource_type=resource_type, n_jobs=n_jobs_to_get)
                        n_jobs_rem -= got_n_jobs
            mainLog.debug("done")
            # check if being terminated
            if self.terminated(harvester_config.jobfetcher.sleepTime):
                mainLog.debug("terminated")
                return
