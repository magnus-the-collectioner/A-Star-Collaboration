#! /usr/bin/env python3

# Copyright 2018 Intel Corporation.
# The source code, information and material ("Material") contained herein is
# owned by Intel Corporation or its suppliers or licensors, and title to such
# Material remains with Intel Corporation or its suppliers or licensors.
# The Material contains proprietary information of Intel or its suppliers and
# licensors. The Material is protected by worldwide copyright laws and treaty
# provisions.
# No part of the Material may be used, copied, reproduced, modified, published,
# uploaded, posted, transmitted, distributed or disclosed in any way without
# Intel's prior express written permission. No license under any patent,
# copyright or other intellectual property rights in the Material is granted to
# or conferred upon you, either expressly, by implication, inducement, estoppel
# or otherwise.
# Any license under such intellectual property rights must be express and
# approved by Intel in writing.

import os
import sys
import argparse
import numpy as np
from Controllers.DataTransforms import *

if sys.version_info[0] != 3:
    sys.stdout.write("Attempting to run with a version of Python != 3.x\n")
    sys.exit(1)

from Controllers.EnumController import *
from Controllers.FileIO import *
from Models.Blob import *
from Models.EnumDeclarations import *
from Models.MyriadParam import *
from Views.Validate import *
from Controllers.Args import coords
import Controllers.Globals as GLOBALS
from Controllers.Scheduler import load_myriad_config, load_network
from Controllers.PingPong import ppInit
from Controllers.Adaptor import getStorageEnum

major_version = np.uint32(2)
release_number = np.uint32(0)


def parse_args():
    parser = argparse.ArgumentParser(description="mvNCCheck.py validates a Caffe or Tensorflow network on the Movidius Neural Compute Stick\n")
    parser.add_argument('network', type=str, help='Network file (.prototxt, .meta, .pb, .protobuf)')
    parser.add_argument('-w', dest='weights', type=str, help='Weights file (override default same name of .protobuf)')
    parser.add_argument('-in', dest='inputnode', type=str, help='Input node name')
    parser.add_argument('-on', dest='outputnode', type=str, help='Output node name')
    parser.add_argument('-i', dest='image', type=str, default='Debug', help='Image to process')
    parser.add_argument('-s', dest='nshaves', type=int, default=1, help='Number of shaves (default 1)')
    parser.add_argument('-is', dest='inputsize', nargs=2, type=int, help='Input size for networks that don\'t provide an input shape, width and height expected')
    parser.add_argument('-S', dest='scale', type=float, help='Scale the input by this amount, before mean')
    parser.add_argument('-M', dest='mean', type=str, help='Numpy file or constant to subtract from the image, after scaling')
    parser.add_argument('-id', dest='expectedid', type=int, help='Expected output id for validation')
    parser.add_argument('-cs', dest='channel_swap', type=coords, default=(2,1,0), help="default: 2,1,0 for RGB-to-BGR; no swap: 0,1,2", nargs='?')
    parser.add_argument('-dn', dest='device_no', metavar='', type=str, nargs='?', help="Experimental flag to run on a specified stick.")
    parser.add_argument('-ec', dest='explicit_concat', action='store_true', help='Force explicit concat')
    parser.add_argument('--accuracy_adjust', type=str, const="ALL:256", default="ALL:1", help='Scale the output by this amount', nargs='?')
    parser.add_argument('--ma2480', action="store_true", help="Dev flag")
    parser.add_argument('--scheduler', action="store", help="Dev flag")
    parser.add_argument('--new-parser', action="store_true", help="Dev flag")
    parser.add_argument('-of', dest='save_output', type=str, default=None,
            help='File name for the myriad result output in numpy format.')
    parser.add_argument('-rof', dest='save_ref_output', type=str, default=None,
            help='File name for the reference result in numpy format')
    parser.add_argument('-metric', dest = 'metric', type = str,
            default = "top5", help = "Metric to be used for validation.\
                    Options: top1, top5 or accuracy_metrics, ssd_pred_metric")
    args = parser.parse_args()
    return args


class Arguments:
    def __init__(self, network, image, inputnode, outputnode, inputsize, nshaves, weights, extargs):
        self.net_description = network
        filetype = network.split(".")[-1]
        self.parser = Parser.TensorFlow
        if filetype in ["prototxt"]:
            self.parser = Parser.Caffe
            if weights is None:
                weights = network[:-8] + 'caffemodel'
                if not os.path.isfile(weights):
                    weights = None
        self.conf_file = network[:-len(filetype)] + 'conf'
        if not os.path.isfile(self.conf_file):
            self.conf_file = None
        self.net_weights = weights
        self.input_node_name = inputnode
        self.output_node_name = outputnode
        self.input_size = inputsize
        self.number_of_shaves = nshaves
        self.image = image
        self.raw_scale = 1
        self.mean = None
        self.channel_swap = None
        self.explicit_concat = extargs.explicit_concat
        self.acm = 0
        self.timer = None
        self.number_of_iterations = 2
        self.upper_temperature_limit = -1
        self.lower_temperature_limit = -1
        self.backoff_time_normal = -1
        self.backoff_time_high = -1
        self.backoff_time_critical = -1
        self.temperature_mode = 'Advanced'
        self.network_level_throttling = 1
        self.stress_full_run = 1
        self.stress_usblink_write = 1
        self.stress_usblink_read = 1
        self.debug_readX = 100
        self.mode = 'validation'
        self.outputs_name = 'output'
        self.save_input = None
        self.save_output = None
        self.device_no = None
        self.exp_id = None
        self.new_parser = extargs.new_parser
        self.seed = -1
        self.scheduler = extargs.scheduler
        self.accuracy_table = {}
        if args.accuracy_adjust != "":
            pairs = args.accuracy_adjust.split(',')
            for pair in pairs:
                layer, value = pair.split(':')
                self.accuracy_table[layer] = float(value)
        if extargs is not None:
            if hasattr(extargs, 'mean') and extargs.mean is not None:
                self.mean = extargs.mean
            if hasattr(extargs, 'scale') and extargs.scale is not None:
                self.raw_scale = extargs.scale
            if hasattr(extargs, 'expectedid') and extargs.expectedid is not None:
                self.exp_id = extargs.expectedid
            if hasattr(extargs, 'channel_swap') and extargs.channel_swap is not None:
                self.channel_swap = extargs.channel_swap
            if hasattr(extargs, 'device_no') and extargs.device_no is not None:
                self.device_no = extargs.device_no
            if hasattr(extargs, 'ma2480') and extargs.ma2480 is not None:
                self.ma2480 = extargs.ma2480
            if hasattr(extargs, 'scheduler') and extargs.scheduler is not None:
                self.scheduler = extargs.scheduler


def check_net(network, image, inputnode=None, outputnode=None, nshaves=1, inputsize=None, weights=None, extargs=None,debug=False):
    file_init()
    args = Arguments(network, image, inputnode, outputnode, inputsize, nshaves, weights, extargs)
    GLOBALS.USING_MA2480 = args.ma2480
    GLOBALS.OPT_SCHEDULER = args.scheduler is None
    ppInit(args.scheduler)

    myriad_config = load_myriad_config(args.number_of_shaves)

    if args.conf_file is not None:
        get_myriad_info(args, myriad_config)
    filetype = network.split(".")[-1]
    parser = None
    if filetype in ["prototxt"]:
        parser = Parser.Caffe
    elif filetype in ["pb", "protobuf", "meta"]:
        parser = Parser.TensorFlow
    else:
        throw_error(ErrorTable.ParserNotSupported)

    file_gen = True
    load_ret = load_network(args, parser, myriad_config,debug=True)
    net = load_ret['network']
    if args.new_parser:
        graph_file = load_ret['graph']
    else:
        graph_file = Blob([GLOBALS.BLOB_MAJOR_VERSION, GLOBALS.BLOB_MINOR_VERSION, GLOBALS.BLOB_PATCH_VERSION], net.name, '', myriad_config, net, "graph")
        graph_file.generate_v2(args)
    timings, myriad_output = run_myriad(graph_file, args)
    report_filename = str(args.outputs_name) + "_val.csv"

    expected = load_ret['expected']
    expected_layout = load_ret['expected_layout']

    # Bring  myriad layout to the layout of the expected output, in order to allow comparison
    # between the two
    if not args.new_parser:
        myriad_output = storage_order_convert(myriad_output, net.outputTensorLayout, expected_layout)

    if file_gen:
        np.save(args.outputs_name + "_result.npy", myriad_output)
        np.save(args.outputs_name + "_expected.npy", expected.astype(dtype=np.float16))

    quit_code = validation(myriad_output, expected, args.exp_id,
            ValidationStatistic[extargs.metric], report_filename, args)

    return quit_code

if __name__ == "__main__":
    setup_warnings()
    print("\033[1mmvNCCheck v" + (u"{0:02d}".format(major_version, )) + "." +
          (u"{0:02d}".format(release_number, )) +
          ", Copyright @ Intel Corporation 2017\033[0m\n")
    args = parse_args()
    quit_code = check_net(args.network, args.image, args.inputnode, args.outputnode, args.nshaves, args.inputsize, args.weights, args)
    quit(quit_code)
