#!/bin/bash
# --input_archive <input_archive> is passed to this script.
# --work_dir <work_dir> is passed to this script.
# --log_dir <log_dir> is passed to this script.
# --batch_executable <batch_executable> is passed to this script.

while [[ $# -gt 0 ]]; do
    case "$1" in
        --input_archive)
            input_archive="$2"
            shift 2
            ;;
        --work_dir)
            work_dir="$2"
            shift 2
            ;;
        --log_dir)
            log_dir="$2"
            shift 2
            ;;
        --batch_executable)
            batch_executable="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1"
            shift
            ;;
    esac
done

mkdir -p ${work_dir}
cd ${work_dir}

if [[ -n "${input_archive}" ]]; then
    echo "Extracting input archive: ${input_archive} into ${work_dir}"
    cp ${input_archive} ${work_dir}/
    echo tar -xzf ${work_dir}/$(basename ${input_archive}) -C ${work_dir}
    tar -xzf ${work_dir}/$(basename ${input_archive}) -C ${work_dir}
fi
if [[ -n "${log_dir}" ]]; then
    echo "Creating log directory: ${log_dir}"
    mkdir -p ${log_dir}
    chmod -R a+rx ${log_dir}
fi

echo sbatch ${work_dir}/${batch_executable}
sbatch ${work_dir}/${batch_executable}

