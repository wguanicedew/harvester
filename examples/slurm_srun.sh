#!/bin/bash
### --ntasks-total=8 --ntasks=1 --cpus-per-task=1 --mem-per-cpu=4000

ntasks_total=1
ntasks=-1
cpus_per_task=-1
mem_per_cpu=-1
input_archive=""
log_dir=""

myargs="$@"

POSITIONAL=()
while [[ $# -gt 0 ]]
do
key="$1"
case $key in
    --input_archive)
    input_archive="$2"
    shift
    shift
    ;;
    --log_dir)
    log_dir="$2"
    shift
    shift
    ;;
    --ntasks-total)
    ntasks_total="$2"
    shift
    shift
    ;;
    --ntasks)
    ntasks="$2"
    shift
    shift
    ;;
    --cpus-per-task)
    cpus_per_task="$2"
    shift
    shift
    ;;
    --mem-per-cpu)
    mem_per_cpu="$2"
    shift
    shift
    ;;
    *)
    POSITIONAL+=("$1") # save it in an array for later
    shift
    ;;
esac
done
set -- "${POSITIONAL[@]}" # restore positional parameters

pilotargs="$@"

cmd="srun"
# if [[ $ntasks -gt 0 ]]; then
#    cmd="$cmd --ntasks $ntasks"
# fi
if [[ ${cpus_per_task} -gt 0 ]]; then
    cmd="$cmd --cpus-per-task ${cpus_per_task}"
fi
if [[ ${mem_per_cpu} -gt 0 ]]; then
    cmd="$cmd --mem-per-cpu ${mem_per_cpu}"
fi

pandaenvdir=/global/cfs/cdirs/m5037/pilot_env

export PANDA_ENV_PILOT_DIR=${pandaenvdir}

echo "pandaenvdir: ${pandaenvdir}"
echo "PANDA_ENV_PILOT_DIR: ${PANDA_ENV_PILOT_DIR}"


pilot_cfg=${pandaenvdir}/pilot/pilot_default.cfg
if [[ -f ${pilot_cfg} ]]; then
    if [[ -z "${HARVESTER_PILOT_CONFIG}" ]]; then
      export HARVESTER_PILOT_CONFIG=${pilot_cfg}
    fi
fi

export PILOT_ES_EXECUTOR_TYPE=fineGrainedProc

# https://datalake-cric.cern.ch/cache/schedconfig/{pandaqueue}.json
# https://datalake-cric.cern.ch/api/atlas/ddmendpoint/query/?json

echo

piloturl=""
local_pilot=${pandaenvdir}/pilot/pilot3.tar.gz
if [[ -f ${local_pilot} ]]; then
    piloturl="--piloturl file://${local_pilot}"
fi

pilot_wrapper=${pandaenvdir}/pilot/wrapper/runpilot3_wrapper.sh
pilot_wrapper_cmd="${pandaenvdir}/pilot/wrapper/runpilot3_wrapper.sh ${piloturl} $@ "

echo $cmd
echo $pilot_wrapper_cmd
echo 

# if input_archive is not empty, we can use srun to launch multiple tasks in parallel
if [[ -n "${input_archive}" ]]; then
    echo "Extracting input archive: ${input_archive}"
    cp ${input_archive} .
    tar -xzf $(basename ${input_archive})
fi
if [[ -n "${log_dir}" ]]; then
    echo "Creating log directory: ${log_dir}"
    mkdir -p ${log_dir}
    chmod -R a+r ${log_dir}
fi
# "executable_batch": batchFile,
# "token_file": token_file,
# "token_vo_file": token_vo_file,
# "x509_proxy": self.x509_proxy,
# "pandaJobData.out": os.path.join(workSpec.accessPoint, "pandaJobData.out")}
# if file x509up is there, set X509_USER_PROXY to x509up
if [[ -f x509_proxy ]]; then
    echo "Found x509_proxy file, setting X509_USER_PROXY to $(pwd)/x509_proxy"
    export X509_USER_PROXY=$(pwd)/x509_proxy
elif [[ -f token_file ]] && [[ -f token_vo_file ]]; then
    echo "Found token_file and token_vo_file, setting PANDA_AUTH_ID_TOKEN and PANDA_AUTH_VO"
    export PANDA_AUTH_ID_TOKEN=$(cat token_file);
    export PANDA_AUTH_VO=$(cat token_vo_file);
    export PANDA_AUTH=oidc;
fi

# ntasks=${ntasks_total}
# for i in $(seq 1 $ntasks); do
#    run_command="$cmd ${pilot_wrapper_cmd}" 
#    $run_cmd | sed -e "s/^/pilot_$i: /"  &
# done
# 
# wait

cat <<EOF > my_panda_run_script
#!/bin/bash

# Ensure SLURM_PROCID is available per task
echo "Task started: pilot_\${SLURM_PROCID} on $(hostname)"

echo ${pilot_wrapper_cmd} | sed -e "s/^/pilot_\${SLURM_PROCID}: /"
echo

${pilot_wrapper_cmd} | sed -e "s/^/pilot_\${SLURM_PROCID}: /"

EOF


chmod +x my_panda_run_script


echo $cmd --export=ALL --ntasks=${ntasks_total} --cpu-bind=none ./my_panda_run_script
echo

$cmd --export=ALL --ntasks=${ntasks_total} --cpu-bind=none ./my_panda_run_script