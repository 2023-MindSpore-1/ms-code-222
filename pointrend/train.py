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
"""train MaskRcnn and get checkpoint files."""

import os
import time
import sys
import mindspore.common.dtype as mstype
from mindspore import context, Tensor, Parameter
from mindspore.communication.management import init, get_rank, get_group_size
from mindspore.train.callback import CheckpointConfig, ModelCheckpoint, TimeMonitor
from mindspore.train import Model
from mindspore.context import ParallelMode
from mindspore.train.serialization import load_checkpoint, load_param_into_net
from mindspore.nn import Momentum
from mindspore.common import set_seed
from maskrcnn.model_utils.config import config
from maskrcnn.model_utils.moxing_adapter import moxing_wrapper
from maskrcnn.model_utils.device_adapter import get_device_id, get_device_num, get_rank_id
from maskrcnn_pointrend.src.maskrcnnPointRend_r50 import maskrcnn_r50_pointrend
from maskrcnn_pointrend.src.network_define import LossCallBack, WithLossCell, TrainOneStepCell, LossNet
from maskrcnn_pointrend.src.dataset import data_to_mindrecord_byte_image, create_maskrcnn_dataset
from maskrcnn_pointrend.src.lr_schedule import dynamic_lr

curPath = os.path.abspath(os.path.dirname(__file__))
rootPath = os.path.split(curPath)[0]
sys.path.append(os.path.split(rootPath)[0])
set_seed(1)

def modelarts_pre_process():
    '''modelarts_pre_process'''
    def unzip(zip_file, save_dir):
        import zipfile
        s_time = time.time()
        if not os.path.exists(os.path.join(save_dir, config.modelarts_dataset_unzip_name)):
            zip_isexist = zipfile.is_zipfile(zip_file)
            if zip_isexist:
                fz = zipfile.ZipFile(zip_file, 'r')
                data_num = len(fz.namelist())
                print("Extract Start...")
                print("unzip file num: {}".format(data_num))
                data_print = int(data_num / 100) if data_num > 100 else 1
                i = 0
                for file in fz.namelist():
                    if i % data_print == 0:
                        print("unzip percent: {}%".format(int(i * 100 / data_num)), flush=True)
                    i += 1
                    fz.extract(file, save_dir)
                print("cost time: {}min:{}s.".format(int((time.time() - s_time) / 60), \
                                                     int(int(time.time() - s_time) % 60)))
                print("Extract Done")
            else:
                print("This is not zip.")
        else:
            print("Zip has been extracted.")

    if config.need_modelarts_dataset_unzip:
        zip_file_1 = os.path.join(config.data_path, config.modelarts_dataset_unzip_name + ".zip")
        save_dir_1 = os.path.join(config.data_path)

        sync_lock = "/tmp/unzip_sync.lock"

        # Each server contains 8 devices as most
        if get_device_id() % min(get_device_num(), 8) == 0 and not os.path.exists(sync_lock):
            print("Zip file path: ", zip_file_1)
            print("Unzip file save dir: ", save_dir_1)
            unzip(zip_file_1, save_dir_1)
            print("===Finish extract data synchronization===")
            try:
                os.mknod(sync_lock)
            except IOError:
                pass

        while True:
            if os.path.exists(sync_lock):
                break
            time.sleep(1)

        print("Device: {}, Finish sync unzip data from {} to {}.".format(get_device_id(), zip_file_1, save_dir_1))
        print("#" * 200, os.listdir(save_dir_1))
        print("#" * 200, os.listdir(os.path.join(config.data_path, config.modelarts_dataset_unzip_name)))

        config.coco_root = os.path.join(config.data_path, config.modelarts_dataset_unzip_name)
    config.save_checkpoint_path = config.output_path
    config.pre_trained = os.path.join(config.output_path, config.pre_trained)


def create_mindrecord_dir(prefix, mindrecord_dir, mindrecord_file):
    '''create_mindrecord_dir'''
    if not os.path.isdir(mindrecord_dir):
        os.makedirs(mindrecord_dir)
    if config.dataset == "coco":
        if os.path.isdir(config.coco_root):
            print("Create Mindrecord.")
            data_to_mindrecord_byte_image("coco", True, prefix, file_num=8)
            print("Create Mindrecord Done, at {}".format(mindrecord_dir))
        else:
            raise Exception("coco_root not exits.")
    else:
        if os.path.isdir(config.IMAGE_DIR) and os.path.exists(config.ANNO_PATH):
            print("Create Mindrecord.")
            data_to_mindrecord_byte_image("other", True, prefix)
            print("Create Mindrecord Done, at {}".format(mindrecord_dir))
        else:
            raise Exception("IMAGE_DIR or ANNO_PATH not exits.")
    while not os.path.exists(mindrecord_file + ".db"):
        time.sleep(5)


def load_pretrained_ckpt(net, load_path, device_target):
    '''load_pretrained_ckpt'''
    param_dict = load_checkpoint(load_path)

    if config.pretrain_epoch_size == 0:
        key_mapping = {'down_sample_layer.1.beta': 'bn_down_sample.beta',
                       'down_sample_layer.1.gamma': 'bn_down_sample.gamma',
                       'down_sample_layer.0.weight': 'conv_down_sample.weight',
                       'down_sample_layer.1.moving_mean': 'bn_down_sample.moving_mean',
                       'down_sample_layer.1.moving_variance': 'bn_down_sample.moving_variance',
                       }
        for oldkey in list(param_dict.keys()):
            if not oldkey.startswith(('backbone', 'end_point', 'global_step',
                                      'learning_rate', 'moments', 'momentum')):
                data = param_dict.pop(oldkey)
                newkey = 'backbone.' + oldkey
                param_dict[newkey] = data
                oldkey = newkey
            for k, v in key_mapping.items():
                if k in oldkey:
                    newkey = oldkey.replace(k, v)
                    param_dict[newkey] = param_dict.pop(oldkey)
                    break

        for item in list(param_dict.keys()):
            if not (item.startswith('backbone') or item.startswith('rcnn_mask')):
                param_dict.pop(item)

        if device_target == 'GPU':
            for key, value in param_dict.items():
                tensor = Tensor(value, mstype.float32)
                param_dict[key] = Parameter(tensor, key)

    load_param_into_net(net, param_dict)
    return net

@moxing_wrapper(pre_process=modelarts_pre_process)
def train_maskrcnn_mobilenetv1():
    '''train_maskrcnn_mobilenetv1'''
    device_target = config.device_target
    context.set_context(mode=context.GRAPH_MODE, device_target=device_target)
    if config.device_target == "Ascend":
        context.set_context(device_id=config.device_id)
    else:
        context.set_context(device_id=0)

    print("Start training for pointrend!")

    dataset_sink_mode_flag = config.dataset_sink_mode_flag
    if not config.do_eval and config.run_distribute:
        init("nccl")
        if config.device_target == "Ascend":
            dataset_sink_mode_flag = True
            rank = get_rank_id()
        else:
            dataset_sink_mode_flag = False
            rank = get_rank()
        set_seed(1)
        device_num = get_group_size()
        context.set_auto_parallel_context(device_num=device_num, parallel_mode=ParallelMode.DATA_PARALLEL,
                                          gradients_mean=True)
    else:
        rank = 0
        device_num = 1

    print("Start create dataset!")

    # It will generate mindrecord file in config.mindrecord_dir,
    # and the file name is MaskRcnn.mindrecord0, 1, ... file_num.
    if config.train_data_type == "val2017":
        prefix = "MaskRcnn_eval.mindrecord"
        mindrecord_dir = config.mindrecord_dir
        mindrecord_file = os.path.join(mindrecord_dir, prefix)
    else:
        prefix = "MaskRcnn.mindrecord"
        mindrecord_dir = config.mindrecord_dir
        mindrecord_file = os.path.join(mindrecord_dir, prefix + "0")
    if rank == 0 and not os.path.exists(mindrecord_file):
        create_mindrecord_dir(prefix, mindrecord_dir, mindrecord_file)

    if not config.only_create_dataset:
        dataset = create_maskrcnn_dataset(mindrecord_file, batch_size=config.batch_size,
                                          device_num=device_num, rank_id=rank)

        dataset_size = dataset.get_dataset_size()
        print("total images num: ", dataset_size)
        print("Create dataset done!")
        net = maskrcnn_r50_pointrend(config=config)
        net = net.set_train()
        load_path = config.pre_trained
        print(f"load_path: {load_path}")
        if load_path != "":
            print("Loading pretrained pointrend checkpoint")
            net = load_pretrained_ckpt(net=net, load_path=load_path, device_target=device_target)
        loss = LossNet()
        lr = Tensor(dynamic_lr(config, rank_size=device_num, start_steps=config.pretrain_epoch_size * dataset_size),
                    mstype.float32)

        for param in net.trainable_params():
            if param.name == 'mask_coarse_head.reduce_spatial_dim_conv.weight': break
            param.requires_grad = False
        params = net.trainable_params()
        opt = Momentum(params=params, learning_rate=lr, momentum=config.momentum,
                       weight_decay=config.weight_decay, loss_scale=config.loss_scale)

        net_with_loss = WithLossCell(net, loss)
        if config.run_distribute:
            net = TrainOneStepCell(net_with_loss, opt, sens=config.loss_scale, reduce_flag=True,
                                   mean=True, degree=device_num)
        else:
            net = TrainOneStepCell(net_with_loss, opt, sens=config.loss_scale)

        time_cb = TimeMonitor(data_size=dataset_size)
        loss_cb = LossCallBack(rank_id=rank)
        cb = [time_cb, loss_cb]

        if config.save_checkpoint and rank == 0:
            ckptconfig = CheckpointConfig(save_checkpoint_steps=int(config.save_checkpoint_epochs * dataset_size),
                                          keep_checkpoint_max=config.keep_checkpoint_max)
            if config.run_distribute:
                p = 'ckpt_dist'
            else:
                p = 'ckpt_'
            save_checkpoint_path = os.path.join(config.save_checkpoint_path, p + str(rank) + '/')
            ckpoint_cb = ModelCheckpoint(prefix='mask_rcnn', directory=save_checkpoint_path, config=ckptconfig)
            cb += [ckpoint_cb]

        model = Model(net)
        model.train(config.epoch_size, dataset, callbacks=cb, dataset_sink_mode=dataset_sink_mode_flag, sink_size=20)

if __name__ == '__main__':
    train_maskrcnn_mobilenetv1()
