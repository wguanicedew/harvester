import os
import sys
import json
import zipfile
import requests

# TO BE REMOVED for python2.7
import requests.packages.urllib3
requests.packages.urllib3.disable_warnings()

from pandaharvester.harvestercore import CoreUtils
from pandaharvester.harvestercore.PluginBase import PluginBase
from pandaharvester.harvesterconfig import harvester_config


# logger
baseLogger = CoreUtils.setupLogger()



# plugin for stager with FTS
class FstStager (PluginBase):
    
    # constructor
    def __init__(self,**kwarg):
        PluginBase.__init__(self,**kwarg)



    # check status
    def checkStatus(self,jobSpec):
        # make logger
        tmpLog = CoreUtils.makeLogger(baseLogger,'PandaID={0}'.format(jobSpec.PandaID))
        tmpLog.debug('start')
        # loop over all files
        trasnferStatus = {}
        for fileSpec in jobSpec.outFiles:
            # get transfer ID
            transferID = fileSpec.fileAttributes['transferID']
            if not transferID in trasnferStatus:
                # get status
                errMsg = None
                try:
                    url = "{0}/jobs/{1}".format(self.ftsServer,
                                                transferID)
                    res = requests.get(url,
                                       timeout=self.ftsLookupTimeout,
                                       verify=self.ca_cert,
                                       cert=(harvester_config.pandacon.cert_file,
                                             harvester_config.pandacon.key_file)
                                       )
                    if res.status_code == 200:
                        transferData = res.json()
                        trasnferStatus[transferID] = transferData["job_state"]
                        tmpLog.debug('got {0} for {1}'.format(trasnferStatus[transferID],
                                                              transferID))
                    else:
                        errMsg = 'StatusCode={0} {1}'.format(res.status_code,
                                                             res.text)
                except:
                    if errMsg == None:
                        errtype,errvalue = sys.exc_info()[:2]
                        errMsg = "{0} {1}".format(errtype.__name__,errvalue)
                # failed
                if errMsg != None:
                    tmpLog.error('failed to get status for {0} with {1}'.format(transferID,
                                                                                errMsg))
                    # set dummy not to lookup again
                    trasnferStatus[transferID] = None
            # final status
            if trasnferStatus[transferID] == 'DONE':
                fileSpec.status = 'finished'
            elif trasnferStatus[transferID] in ['FAILED','CANCELED']:
                fileSpec.status = 'failed'
        return True,''



    # trigger stage out
    def triggerStageOut(self,jobSpec):
        # make logger
        tmpLog = CoreUtils.makeLogger(baseLogger,'PandaID={0}'.format(jobSpec.PandaID))
        tmpLog.debug('start')
        # default return
        tmpRetVal = (True,'')
        # loop over all files
        files = []
        lfns = set()
        for fileSpec in jobSpec.outFiles:
            # skip zipped files
            if fileSpec.zipFileID != None:
                continue
            # source and destination URLs
            if fileSpec.fileType == 'es_output':
                srcURL = self.srcEndpointES + fileSpec.path
                dstURL = self.dstEndpointES + fileSpec.path
                # set OS ID
                fileSpec.objstoreID = self.esObjStoreID
            else:
                scope = jobSpec.getOutputFileAttributes[fileSpec.lfn]
                hash = hashlib.md5()
                hash.update('%s:%s' % (scope,fileSpec.lfn))
                hash_hex = hash.hexdigest()
                correctedscope = "/".join(scope.split('.'))
                if fileSpec.fileType == 'output':
                    srcURL = self.srcEndpointOut + fileSpec.path
                    dstURL = "{endPoint}/{scope}/{hash1}/{hash2}/{lfn}".format(endPoint=self.dstEndpointOut,
                                                                               scope=correctedscope,
                                                                               hash1=hash_hex[0:2],
                                                                               hash2=hash_hex[2:4],
                                                                               lfn=fileSpec.lfn)
                elif fileSpec.fileType == 'log':
                    # skip if no endpoint
                    if self.srcEndpointLog == None:
                        continue
                    srcURL = self.srcEndpointLog + fileSpec.path
                    dstURL = "{endPoint}/{scope}/{hash1}/{hash2}/{lfn}".format(endPoint=self.dstEndpointLog,
                                                                               scope=correctedscope,
                                                                               hash1=hash_hex[0:2],
                                                                               hash2=hash_hex[2:4],
                                                                               lfn=fileSpec.lfn)
                else:
                    continue
            tmpLog.debug('src={srcURL} dst={dstURL}'.format(srcURL=srcURL,dstURL=dstURL))
            files.append({
                    "sources":[srcURL],
                    "destinations":[dstURL],
                    })
            lfns.add(fileSpec.lfn)
        # submit
        if files != []:
                # get status
                errMsg = None
                try:
                    url = "{0}/jobs".format(self.ftsServer)
                    res = requests.post(url,
                                        json={"Files":files},
                                        timeout=self.ftsLookupTimeout,
                                        verify=self.ca_cert,
                                        cert=(harvester_config.pandacon.cert_file,
                                              harvester_config.pandacon.key_file)
                                        )
                    if res.status_code == 200:
                        transferData = res.json()
                        transferID = transferData["job_id"]
                        tmpLog.debug('successfully submitted id={0}'.format(transferID))
                        # set
                        for fileSpec in jobSpec.outFiles:
                            if fileSpec.fileAttributes == None:
                                fileSpec.fileAttributes = {}
                            fileSpec.fileAttributes['transferID'] = transferID
                    else:
                        # HTTP error
                        errMsg = 'StatusCode={0} {1}'.format(res.status_code,
                                                             res.text)
                except:
                    if errMsg == None:
                        errtype,errvalue = sys.exc_info()[:2]
                        errMsg = "{0} {1}".format(errtype.__name__,errvalue)
                # failed
                if errMsg != None:
                    tmpLog.error('failed to submit transfer with {0}'.format(errMsg))
                    tmpRetVal = (False,errStr)
        # return
        tmpLog.debug('done')
        return tmpRetVal



    # zip output files
    def zipOutput(self,jobSpec):
        # make logger
        tmpLog = CoreUtils.makeLogger(baseLogger,'PandaID={0}'.format(jobSpec.PandaID))
        tmpLog.debug('start')
        try:
            for fileSpec in jobSpec.outFiles:
                zipPath = os.path.join(self.zipDir,fileSpec.lfn)
                # remove zip file just in case
                try:
                    os.remove(zipPath)
                except:
                    pass
                # make zip file
                with zipfile.ZipFile(zipPath, "w", zipfile.ZIP_STORED) as zf:
                    for assFileSpec in fileSpec.associatedFiles:
                        zf.write(assFileSpec.path)
                # set path
                fileSpec.path = zipPath
                # get size
                statInfo = os.stat(zipPath)
                fileSpec.fsize = statInfo.st_size
        except:
            errtype,errvalue = sys.exc_info()[:2]
            errMsg = "{0} {1}".format(errtype.__name__,errvalue)
            tmpLog.error('failed to zip with {0}'.format(errMsg))
            return False,errMsg
        tmpLog.debug('done')
        return True,''

