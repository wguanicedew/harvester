import uuid

from pandaharvester.harvestercore import core_utils
from pandaharvester.harvestercore.plugin_base import PluginBase

# setup base logger
baseLogger = core_utils.setup_logger()


# dummy submitter
class DummySubmitter(PluginBase):
    # constructor
    def __init__(self, **kwarg):
        PluginBase.__init__(self, **kwarg)

    # submit workers
    def submit_workers(self, workspec_list):
        tmpLog = core_utils.make_logger(baseLogger)
        tmpLog.debug('start nWorkers={0}'.format(len(workspec_list)))
        retList = []
        retStrList = []
        for workSpec in workspec_list:
            for jobSpec in workSpec.get_jobspec_list():
                tmpLog.debug('PandaID={0} nCore={1} RAM={2}'.format(jobSpec.PandaID,
                                                                    jobSpec.jobParams['coreCount'],
                                                                    jobSpec.jobParams['minRamCount']))
            workSpec.batchID = uuid.uuid4().hex
            retList.append((True, ''))
        tmpLog.debug('done')
        return retList
