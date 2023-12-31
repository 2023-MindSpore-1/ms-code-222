#!/bin/bash
# Copyright 2022 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================

if [ $# != 4 ] && [ $# != 5 ]
then
    echo "Usage: bash run_eval.sh [DEVICE_ID] [DATA_PATH] [MINDRECORD_PATH] [CHECHPOINT_PATH] [NOT_MASK](optional)"
    exit 1
fi

get_real_path(){
    if [ "${1:0:1}" == "/" ]; then
        echo "$1"
    else
        echo "$(realpath -m $PWD/$1)"
    fi
}

PATH1=$(get_real_path $2)
PATH2=$3
PATH3=$4
not_mask=False
echo "path1: $PATH1"
echo "path2: $PATH2"
echo "path3: $PATH3"

if [ $# == 5 ]
then
    not_mask=$5
fi
echo "not_mask: $not_mask"

if [ ! -d $PATH1 ]
then
    echo "error: DATA_PATH=$PATH1 is not a folder"
    exit 1
fi

export DEVICE_NUM=1
export CUDA_VISIBLE_DEVICES=$1
export RANK_ID=0
export RANK_SIZE=1

cd ..
if [ ! -d "./eval_log" ]
then
    echo "pwd: $(pwd)"
    mkdir ./eval_log
fi
cd ./eval_log

python -u ../eval.py --device_target="GPU" --do_eval=True --device_num=$DEVICE_NUM --not_mask=$not_mask \
--coco_root=$PATH1 --mindrecord_dir=$PATH2 --checkpoint_path=$PATH3 >> eval.log 2>&1 &
