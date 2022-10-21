set -e
CUR_DIR=$(cd `dirname $0`; pwd)

# Setup directories
: "${WORK_DIR:=$CUR_DIR}"
: "${LOG_DIR:=$WORK_DIR/logs}"
mkdir -p "${WORK_DIR}"
mkdir -p "${LOG_DIR}"

# Vars 
: "${DATASETS_DIR:="/algo/modelzoo/datasets/"}"   
: "${CONT:="yellow.hub.cambricon.com/cair_modelzoo/gpu_golden_bench:enet-21.06-pytorch-py3"}"
: "${DOCKERFILE:="./PyTorch/ENet/Dockerfile"}"
: "${DATESTAMP:=$(date +'%y%m%d%H%M%S%N')}"

# Build test docker images
CONT_REPO=$(echo ${CONT} | awk -F: '{print $1}') 
CONT_TAG=$(echo ${CONT} | awk -F: '{print $2}') 
CONT_FIND=$(docker images | grep ${CONT_REPO} | grep ${CONT_TAG}) || true
if [ ! -n "$CONT_FIND" ]
then
  docker build -t ${CONT} -f ${DOCKERFILE} .
else
  echo -e "\033[33m Docker image is found locally! \033[0m"
fi

readonly docker_image=${CONT:-"nvcr.io/SET_THIS_TO_CORRECT_CONTAINER_TAG"}
CONT_NAME="gpu_golden_bench_lidar_rcnn"


# Start container
docker run --rm --detach --gpus all --net=host --ipc=host --shm-size=64g --ulimit memlock=-1 \
--ulimit stack=67108864 -v ${DATASETS_DIR}:/data  \
--name "${CONT_NAME}" "${docker_image}" sleep infinity
#make sure container has time to finish initialization
echo -e "\033[33m Training container is creating... \033[0m"
sleep 10
docker exec -it "${CONT_NAME}" true

device_count=${1:-"8"}
# Run the model
if [[ $device_count == 1 ]]
then
    docker exec -it "${CONT_NAME}"  bash -c "python -m torch.distributed.launch --nproc_per_node=1 tools/train.py --cfg config/lidar_rcnn_2x.yaml --name lidar_rcnn && mkdir results && python -m torch.distributed.launch --nproc_per_node=8 tools/test.py --cfg config/lidar_rcnn.yaml --checkpoint outputs/lidar_rcnn_2x/checkpoint_lidar_rcnn_59.pth.tar && python tools/create_results.py --cfg config/lidar_rcnn.yaml " \
    2>&1 | tee ${LOG_DIR}/lidar_rcnn_Convergency_${DATESTAMP}.log
else
    docker exec -it "${CONT_NAME}"  bash -c "python -m torch.distributed.launch --nproc_per_node=8 tools/train.py --cfg config/lidar_rcnn.yaml --name lidar_rcnn && mkdir results && python -m torch.distributed.launch --nproc_per_node=8 tools/test.py --cfg config/lidar_rcnn.yaml --checkpoint outputs/lidar_rcnn/checkpoint_lidar_rcnn_59.pth.tar && python tools/create_results.py --cfg config/lidar_rcnn.yaml " \
    2>&1 | tee ${LOG_DIR}/lidar_rcnn_Convergency_${DATESTAMP}.log
fi

echo -e "\033[32m Run experiments done! \033[0m"
echo "You need to create the submission file by './results/val.bin' in the container"