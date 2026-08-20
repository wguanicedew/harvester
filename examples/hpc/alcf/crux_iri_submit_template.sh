#!/bin/sh

# The NERSC one, will update it

#SBATCH --account=m2616
#SBATCH --constraint=cpu
#SBATCH --qos=regular
##SBATCH --qos=premium
#SBATCH --nodes={nNode}
##SBATCH --time 48:00:00 # only 2 guaranteed w/ preempt
#SBATCH --time 24:00:00 # only 2 guaranteed w/ preempt
#SBATCH --time-min=6:00:00  # require at least 
#SBATCH --module=cvmfs
#SBATCH --signal=SIGTERM@240
#SBATCH --no-requeue
#SBATCH --job-name=IRI_MCORE
##SBATCH -L SCRATCH,project
## Next are needed for shifter
###SBATCH --image=registry.cern.ch/atlasadc/atlas-grid-centos7:latest
###SBATCH --image=registry.cern.ch/atlasadc/atlas-grid-almalinux9:latest
##SBATCH --volume="/global/common/software:/global/common/software"
#SBATCH --export=NONE

#SBATCH -o {stdout_path}
#SBATCH -e {stderr_path}

## end of shifter SBATCH extentions

export TZ=UTC0

echo [$(date -u "+%m-%d-%y %H:%M:%S %Z")] Start_SLURM_Job


export PANDA_QUEUE={pandaQueueName}
export HARVESTER_DIR=/global/common/software/m2616/harvester-perlmutter/venv/py_3_13_11

export HARVESTER_ACCESS_POINT={work_dir}
export HARVESTER_TASKS_PER_NODE=4 # Should be equal to: (nJobsPerWorker * nCorePerNode) / nCore
export HARVESTER_NNODE={nNode}
export HARVESTER_NTASKS=$((HARVESTER_TASKS_PER_NODE * HARVESTER_NNODE))
export HARVESTER_MAPTYPE=NoJob

export PANDA_JSID=harvester-{harvesterID}
export HARVESTER_ID={harvesterID}
export HARVESTER_WORKER_ID={workerID}
export GTAG={gtag}
export APFMON=http://apfmon.lancs.ac.uk/api
export APFFID={harvesterID}

if [ -n "{pandaTokenFilename}" ] && [ -f "{pandaTokenFilename}" ] && [ -n "{pandaTokenKeyFilename}" ] && [ -f "{pandaTokenKeyFilename}" ]; then
    export PANDA_AUTH_ORIGIN={tokenOrigin}
    export PANDA_AUTH_TOKEN=$(pwd)/{pandaTokenFilename}
    export PANDA_AUTH_TOKEN_KEY=$(pwd)/{pandaTokenKeyFilename}
    # export PANDA_AUTH_ID_TOKEN=$(cat $PANDA_AUTH_TOKEN)
fi

if [ -n "{x509UserProxy}" ] && [ -f "{x509UserProxy}" ]; then
    export X509_USER_PROXY=$(pwd)/{x509UserProxy}
fi

if [[ -n "${input_archive}" ]]; then
    echo "Extracting input archive: ${input_archive}"
    cp ${input_archive} .
    echo tar -xzf $(basename ${input_archive})
    tar -xzf $(basename ${input_archive})
fi

# Careful, bash can only do integer math.
export ATHENA_PROC_NUMBER_JOB=$((256 / (HARVESTER_TASKS_PER_NODE)))
export ATHENA_PROC_NUMBER=$((256 / (HARVESTER_TASKS_PER_NODE)))
export ATHENA_CORE_NUMBER=$((256 / (HARVESTER_TASKS_PER_NODE)))


#DPB_shifter export wrapper_wrapper_file=$HARVESTER_DIR/etc/panda/wrapper-wrapper-3-shifter.sh
export wrapper_wrapper_file=$HARVESTER_DIR/etc/panda/wrapper-wrapper-3.sh

echo [$(date -u "+%m-%d-%y %H:%M:%S %Z")] "Copy $wrapper_wrapper_file into $HARVESTER_ACCESS_POINT"
#DPB_shifter cp -v $wrapper_wrapper_file $HARVESTER_ACCESS_POINT/wrapper-wrapper-3-shifter.sh
cp -v $wrapper_wrapper_file $HARVESTER_ACCESS_POINT/wrapper-wrapper-3.sh

#DPB_shifter echo [$(date -u "+%m-%d-%y %H:%M:%S %Z")] srun --label -n $HARVESTER_NTASKS /usr/bin/shifter /bin/bash ./wrapper-wrapper-3-shifter.sh $PANDA_QUEUE $HARVESTER_ACCESS_POINT
#DPB_shifter srun --label -n $HARVESTER_NTASKS /usr/bin/shifter /bin/bash ./wrapper-wrapper-3-shifter.sh $PANDA_QUEUE $HARVESTER_ACCESS_POINT
#echo [$(date -u "+%m-%d-%y %H:%M:%S %Z")] srun --label -n $HARVESTER_NTASKS  /bin/bash ./wrapper-wrapper-3.sh $PANDA_QUEUE $HARVESTER_ACCESS_POINT
#srun --label -n $HARVESTER_NTASKS  /bin/bash ./wrapper-wrapper-3.sh $PANDA_QUEUE $HARVESTER_ACCESS_POINT
echo [$(date -u "+%m-%d-%y %H:%M:%S %Z")] srun --export=HARVESTER_ID,HARVESTER_WORKER_ID,PANDA_AUTH_ORIGIN,PANDA_AUTH_TOKEN --label -n $HARVESTER_NTASKS  /bin/bash ./wrapper-wrapper-3.sh $PANDA_QUEUE $HARVESTER_ACCESS_POINT
srun --export=HARVESTER_ID,HARVESTER_WORKER_ID,PANDA_AUTH_ORIGIN,PANDA_AUTH_TOKEN --label -n $HARVESTER_NTASKS  /bin/bash ./wrapper-wrapper-3.sh $PANDA_QUEUE $HARVESTER_ACCESS_POINT
wait

chown -R :m2616 $HARVESTER_ACCESS_POINT



#SBATCH -o {remote_log_dir}/stdout.txt
#SBATCH -e {remote_log_dir}/stderr.txt


export PROJ_DIR=/project/projectdirs/m5037
source $PROJ_DIR/pilot/grid_env/setup.sh

export PILOT_DIR=/global/cdf/cdir/m5037/
export PYTHONPATH=$PILOT_DIR:$PYTHONPATH

export WORK_DIR={accessPoint}
cd $WORK_DIR

srun -N {nNode} python-mpi $PILOT_DIR/HPC/HPCJob.py \
    --globalWorkingDir=$WORK_DIR  --localWorkingDir=$WORK_DIR --dumpEventOutputs \
    --dumpFormat json

x509userproxy = {x509UserProxy}
environment = "PANDA_JSID=harvester-{harvesterID} HARVESTER_ID={harvesterID} HARVESTER_WORKER_ID={workerID} GTAG={gtag} APFMON=http://apfmon.lancs.ac.uk/api APFFID={harvesterID} APFCID=$(Cluster).$(Process) PANDA_AUTH_ORIGIN=atlas.pilot PANDA_AUTH_TOKEN={pandaTokenFilename} PANDA_AUTH_TOKEN_KEY={pandaTokenKeyFilename}"
transfer_input_files = {pandaTokenPath},{pandaTokenKeyPath}