import os
import re

from concurrent.futures import ThreadPoolExecutor

from pandaharvester.harvestercore import core_utils
from pandaharvester.harvestercore.plugin_base import PluginBase
from pandaharvester.harvestermisc.k8s_utils import k8s_Client
from pandaharvester.harvesterconfig import harvester_config

# logger
base_logger = core_utils.setup_logger('k8s_submitter')

DEF_SLC6_IMAGE = 'atlasadc/atlas-grid-slc6'
DEF_CENTOS7_IMAGE = 'atlasadc/atlas-grid-centos7'
DEF_IMAGE = DEF_CENTOS7_IMAGE


# submitter for K8S
class K8sSubmitter(PluginBase):
    # constructor
    def __init__(self, **kwarg):
        self.logBaseURL = None
        PluginBase.__init__(self, **kwarg)

        self.k8s_client = k8s_Client(self.k8s_namespace, config_file=self.k8s_config_file)

        # number of processes
        try:
            self.nProcesses
        except AttributeError:
            self.nProcesses = 1
        else:
            if (not self.nProcesses) or (self.nProcesses < 1):
                self.nProcesses = 1
        # x509 proxy
        try:
            self.x509UserProxy
        except AttributeError:
            if os.getenv('X509_USER_PROXY'):
                self.x509UserProxy = os.getenv('X509_USER_PROXY')

        # CPU adjust ratio
        try:
            self.cpuAdjustRatio
        except AttributeError:
            self.cpuAdjustRatio = 100

        # Memory adjust ratio
        try:
            self.memoryAdjustRatio
        except AttributeError:
            self.memoryAdjustRatio = 100

    def extract_container_image_from_params(self, job_params):
        """
        Based on P. Nilsson pilot 2 code:
            https://github.com/PanDAWMS/pilot2/blob/fd71c5010f789d408d366594793f025caba02ab3/pilot/info/jobdata.py#L536
        Extract the container image from the job parameters if present, and remove it.
        :param jobparams: job parameters (string).
        :return: extracted image name (string).
        """
        tmp_log = self.make_logger(base_logger, method_name='submit_k8s_worker')

        # define regexp pattern for the full container image option
        _pattern = r'(\ \-\-containerImage\=?\s?[\S]+)'
        pattern = re.compile(_pattern)
        image_option = re.findall(pattern, job_params)

        if image_option and image_option[0]:

            image_pattern = re.compile(r" \'?\-\-containerImage\=?\ ?([\S]+)\ ?\'?")
            image = re.findall(image_pattern, job_params)
            if image and image[0]:
                image_name = image[0]
                tmp_log.info('Extracted image from jobparams: {0}'.format(image_name))
            else:
                image_name = None
                tmp_log.warning("Image could not be extracted from jobparams:".format(job_params))

        return image_name

    def extract_container_image_from_cmtconfig(self, cmt_config):
        try:
            requested_os = cmt_config.split('@')[1]
            if 'slc6' in requested_os.lower():
                container_image = DEF_SLC6_IMAGE
            else:
                container_image = DEF_CENTOS7_IMAGE
        except KeyError:
            container_image = DEF_IMAGE

        if container_image:
            return container_image

    def decide_container_image(self, work_spec):
        """
        Decide container image:
        - job defined image: if we are running in push mode and the job specified an image, use it
        - production images: take SLC6 or CentOS7
        - otherwise take default image specified for the queue
        """

        job_spec_list = work_spec.get_jobspec_list()
        if job_spec_list:
            job_spec = job_spec_list[0]

            # look for a job defined image
            job_params = job_spec.jobParams
            container_image = self.extract_container_image_from_params(job_params)
            if container_image:
                return container_image

            # check the CMTconfig field
            if 'cmtconfig' in job_spec.jobAttributes:
                cmt_config = job_spec.jobAttributes['cmtconfig']
                container_image = self.extract_container_image_from_cmtconfig(cmt_config)
                return container_image

        # we didn't figure out which image to use, just use the default image
        return DEF_IMAGE

    def build_executable(self, work_spec):

        return executable

    def submit_k8s_worker(self, work_spec):
        tmp_log = self.make_logger(base_logger, method_name='submit_k8s_worker')

        # set the stdout log file
        log_file_name = '{0}_{1}.out'.format(harvester_config.master.harvester_id, work_spec.workerID)
        work_spec.set_log_file('stdout', '{0}/{1}'.format(self.logBaseURL, log_file_name))
        # TODO: consider if we want to upload the yaml file to PanDA cache

        yaml_content = self.k8s_client.read_yaml_file(self.k8s_yaml_file)
        try:

            # decide container image to run
            container_image = self.decide_container_image(work_spec)

            # build executable
            executable = self.build_executable(work_spec)

            if hasattr(self, 'proxySecretPath'):
                cert = self.proxySecretPath
                use_secret = True
            elif hasattr(self, 'x509UserProxy'):
                cert = self.x509UserProxy
                use_secret = False
            else:
                err_str = 'No proxy specified in proxySecretPath or x509UserProxy; not submitted'
                tmp_return_value = (False, err_str)
                return tmp_return_value

            rsp, yaml_content_final = self.k8s_client.create_job_from_yaml(yaml_content, work_spec, container_image,
                                                                           cert, use_secret, self.cpuAdjustRatio,
                                                                           self.memoryAdjustRatio)
        except Exception as _e:
            err_str = 'Failed to create a JOB; {0}'.format(_e)
            tmp_return_value = (False, err_str)
        else:
            work_spec.batchID = yaml_content['metadata']['name']
            tmp_log.debug('Created worker {0} with batchID={1}'.format(work_spec.workerID, work_spec.batchID))
            tmp_return_value = (True, '')

        return tmp_return_value

    # submit workers
    def submit_workers(self, workspec_list):
        tmp_log = self.make_logger(base_logger, method_name='submit_workers')

        n_workers = len(workspec_list)
        tmp_log.debug('start, n_workers={0}'.format(n_workers))

        ret_list = list()
        if not workspec_list:
            tmp_log.debug('empty workspec_list')
            return ret_list

        with ThreadPoolExecutor(self.nProcesses) as thread_pool:
            ret_val_list = thread_pool.map(self.submit_k8s_worker, workspec_list)
            tmp_log.debug('{0} workers submitted'.format(n_workers))

        ret_list = list(ret_val_list)

        tmp_log.debug('done')

        return ret_list
