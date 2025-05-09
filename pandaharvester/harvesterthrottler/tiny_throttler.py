import datetime

from pandaharvester.harvestercore import core_utils
from pandaharvester.harvestercore.db_proxy_pool import DBProxyPool as DBProxy
from pandaharvester.harvestercore.plugin_base import PluginBase

# logger
baseLogger = core_utils.setup_logger("tiny_throttler")


# Tiny throttler
class TinyThrottler(PluginBase):
    # constructor
    def __init__(self, **kwarg):
        # logic type : AND: throttled if all rules are satisfied, OR: throttled if one rule is satisfied
        self.logicType = "OR"
        PluginBase.__init__(self, **kwarg)
        self.dbProxy = DBProxy()

        if not hasattr(self, "rulesForSiteFamilySubmitted"):
            self.rulesForSiteFamilySubmitted = {}
        if not hasattr(self, "rulesForSiteFamilyRunning"):
            self.rulesForSiteFamilyRunning = {}
        if not hasattr(self, "rulesForSiteFamilySubmittedRunning"):
            self.rulesForSiteFamilySubmittedRunning = {}

    def get_num_workers(self, worker_stats, status):
        num_workers = 0
        for job_type in worker_stats:
            for resource_type in worker_stats[job_type]:
                for st in worker_stats[job_type][resource_type]:
                    if st in status:
                        n_workers = worker_stats[job_type][resource_type][st]
                        num_workers += n_workers
        return num_workers

    def get_core_factor(self, q_config, job_type, resource_type):
        try:
            nCoreFactor = q_config.submitter.get("nCoreFactor", 1)
            if type(nCoreFactor) in [dict]:
                n_core_factor = nCoreFactor.get(job_type, {}).get(resource_type, 1)
                return int(n_core_factor)
            return int(nCoreFactor)
        except Exception as ex:
            # logger.warning(f"Failed to get core factor: {ex}")
            pass
        return 1

    def get_num_cores(self, worker_stats, status, q_config):
        num_cores = 0
        for job_type in worker_stats:
            for resource_type in worker_stats[job_type]:
                for st in worker_stats[job_type][resource_type]:
                    if st in status:
                        n_workers = worker_stats[job_type][resource_type][st]
                        n_core_factor = self.get_core_factor(q_config, job_type, resource_type)
                        num_cores += n_workers * n_core_factor
        return num_cores

    def evaluate_rule(self, rules, queues, retVal, status=['submitted']):
        for rule in rules:
            if rule['level'] == 'cores':
                total_cores = 0
                for queue_name in queues:
                    worker_stats = queues[queue_name]['stats']
                    q_config = queues[queue_name]['queue_config']
                    n_cores = self.get_num_cores(worker_stats, status, q_config)
                    total_cores += n_cores
                maxCores = rule['maxCores']
                if total_cores > maxCores:
                    if self.logicType == "OR":
                        tmpMsg = f"logic={self.logicType} and "
                        tmpMsg += f"total_cores={total_cores} > maxCores={maxCores} for {str(rule)}"
                        retVal = True, tmpMsg
                        break
                else:
                    if self.logicType == "AND":
                        tmpMsg = f"logic={self.logicType} and "
                        tmpMsg += f"total_cores={total_cores} > maxCores={maxCores} for {str(rule)}"
                        retVal = False, tmpMsg
                        break
            elif rule['level'] == 'workers':
                total_workers = 0
                for queue_name in queues:
                    worker_stats = queues[queue_name]['stats']
                    num_workers = self.get_num_workers(worker_stats, status)
                    total_workers += num_workers
                maxWorkers = rule['maxWorkers']
                if total_workers > maxWorkers:
                    if self.logicType == "OR":
                        tmpMsg = f"logic={self.logicType} and "
                        tmpMsg += f"total_workers={total_workers} > maxWorkers={maxWorkers} for {str(rule)}"
                        retVal = True, tmpMsg
                        break
                else:
                    if self.logicType == "AND":
                        tmpMsg = f"logic={self.logicType} and "
                        tmpMsg += f"total_workers={total_workers} <= maxWorkers={maxWorkers} for {str(rule)}"
                        retVal = False, tmpMsg
                        break
        return retVal

    # check if to be throttled
    def to_be_throttled(self, queue_config, queue_config_mapper=None):
        tmpLog = self.make_logger(baseLogger, f"computingSite={queue_config.queueName}", method_name="to_be_throttled")
        tmpLog.debug("start")
        # set default return vale
        if self.logicType == "OR":
            retVal = False, "no rule was satisfied"
        else:
            retVal = True, "all rules were satisfied"
        # loop over all rules
        criteriaList = []
        maxMissedList = []
        timeNow = datetime.datetime.utcnow()
        for rule in self.rulesForMissed:
            # convert rule to criteria
            if rule["level"] == "site":
                criteria = dict()
                criteria["siteName"] = queue_config.siteName
                criteria["timeLimit"] = timeNow - datetime.timedelta(minutes=rule["timeWindow"])
                criteriaList.append(criteria)
                maxMissedList.append(rule["maxMissed"])
            elif rule["level"] == "pq":
                criteria = dict()
                criteria["computingSite"] = queue_config.queueName
                criteria["timeLimit"] = timeNow - datetime.timedelta(minutes=rule["timeWindow"])
                criteriaList.append(criteria)
                maxMissedList.append(rule["maxMissed"])
            elif rule["level"] == "ce":
                elmName = "computingElements"
                if elmName not in queue_config.submitter:
                    tmpLog.debug(f"skipped since {elmName} is undefined in submitter config")
                    continue
                for ce in queue_config.submitter[elmName]:
                    criteria = dict()
                    criteria["computingElement"] = ce
                    criteria["timeLimit"] = timeNow - datetime.timedelta(minutes=rule["timeWindow"])
                    criteriaList.append(criteria)
                    maxMissedList.append(rule["maxMissed"])
        # loop over all criteria
        for criteria, maxMissed in zip(criteriaList, maxMissedList):
            nMissed = self.dbProxy.get_num_missed_workers(queue_config.queueName, criteria)
            if nMissed > maxMissed:
                if self.logicType == "OR":
                    tmpMsg = f"logic={self.logicType} and "
                    tmpMsg += f"nMissed={nMissed} > maxMissed={maxMissed} for {str(criteria)}"
                    retVal = True, tmpMsg
                    break
            else:
                if self.logicType == "AND":
                    tmpMsg = f"logic={self.logicType} and "
                    tmpMsg += f"nMissed={nMissed} <= maxMissed={maxMissed} for {str(criteria)}"
                    retVal = False, tmpMsg
                    break
        tmpLog.debug("rulesForMissed ret={0} : {1}".format(*retVal))
        if retVal[0]:
            return retVal

        if self.rulesForSiteFamilySubmitted or self.rulesForSiteFamilyRunning or self.rulesForSiteFamilySubmittedRunning:
            site_name = queue_config.siteFamily
            queues = {}

            job_stats = self.dbProxy.get_worker_stats_bulk(None)

            tmpLog.debug("site family name: %s" % site_name)

            all_queue_config = queue_config_mapper.get_all_queues()
            for queue_name in all_queue_config:
                q_config = all_queue_config[queue_name]
                if q_config.siteFamily == site_name:
                    queues[queue_name] = {'queue_config': q_config, 'stats': {}}
                    if queue_name in job_stats:
                        queues[queue_name]['stats'] = job_stats[queue_name]

            tmpLog.debug("queues: %s" % queues)

            if self.rulesForSiteFamilySubmitted:
                retVal = self.evaluate_rule(self.rulesForSiteFamilySubmitted, queues, retVal, status=['submitted'])
                tmpLog.debug("rulesForSiteFamilySubmitted ret={0} : {1}".format(*retVal))
                if retVal[0]:
                    return retVal
            if self.rulesForSiteFamilyRunning:
                retVal = self.evaluate_rule(self.rulesForSiteFamilyRunning, queues, retVal, status=['running'])
                tmpLog.debug("rulesForSiteFamilyRunning ret={0} : {1}".format(*retVal))
                if retVal[0]:
                    return retVal
            if self.rulesForSiteFamilySubmittedRunning:
                retVal = self.evaluate_rule(self.rulesForSiteFamilySubmittedRunning, queues, retVal, status=['submitted', 'running'])
                tmpLog.debug("rulesForSiteFamilySubmittedRunning ret={0} : {1}".format(*retVal))
                if retVal[0]:
                    return retVal
        return retVal
