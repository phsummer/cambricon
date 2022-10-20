#!/bin/bash
set -e
CUR_DIR=$(cd $(dirname $0);pwd)
python -m torch.distributed.launch --nproc_per_node=2 $CUR_DIR/../tools/train.py $CUR_DIR/../configs/mask_rcnn/mask_rcnn_r101_fpn_1x_coco.py --no-validate --enable_device mlu --launcher mlu --cnmix O0 --seed 0
