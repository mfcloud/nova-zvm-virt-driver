# Copyright 2016 IBM Corp.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_config import cfg


zvm_opts = [
    cfg.IntOpt('zvm_console_log_size',
               default=100,
               help="""
The maximum console log size, in kilobytes, allowed.

Console logs must be transferred to OpenStack from z/VM side,
so this controls how large each transferred console can be.

Possible values:
    Any positive integer, recommended to be at least 100 KB to
    avoid unnecessary calls between z/VM and OpenStack.
"""),
    ]

zvm_user_opts = [
    ]

zvm_image_opts = [
    cfg.StrOpt('zvm_image_tmp_path',
               default='/var/lib/nova/images',
               help="""
The path at which images will be stored (snapshot, deploy, etc).

The image used to deploy or image captured from instance need to be
stored in local disk of compute node host. This configuration identifies
the directory location.

Possible values:
    A path in host that running compute service.
"""),
    cfg.StrOpt('zvm_default_ephemeral_mntdir',
               default='/mnt/ephemeral',
               help='The path to which the ephemeral disk be mounted'),
    cfg.StrOpt('zvm_image_default_password',
               default='rootpass',
               secret=True,
               help='Default os root password for a new created vm'),
    cfg.IntOpt('xcat_image_clean_period',
               default=30,
               help="""
Number of days an unused xCAT image will be retained before it is purged.

Copies of Glance images are kept in the xCAT MN to make deploys faster.
Unused images are purged to reclaim disk space. If an image has been purged
from xCAT, the next deploy will take slightly longer as it must be copied
from OpenStack into xCAT.

Possible values:
    Any positive integer, recommended to be at least 30 (1 month).
"""),
    cfg.IntOpt('xcat_free_space_threshold',
               default=50,
               help='The threshold for xCAT free space, if snapshot or spawn '
                     'check xCAT free space not enough for its image '
                     'operations, it will prune image to meet the threshold'),
    cfg.StrOpt('zvm_image_compression_level',
               default=None,
               help="""
The level of gzip compression used when capturing disk.

A snapshotted image will consume disk space on xCAT MN host and the OpenStack
compute host. To save disk space the image should be compressed.
The zvm driver uses gzip to compress the image. gzip has a set of different
levels depending on the speed and quality of compression.
For more information, please refer to the -N option of the gzip command.

Possible values:
    An integer between 0 and 9, where 0 is no compression and 9 is the best,
    but slowest compression. A value of "None" will result in the default
    compression level, which is currently '6' for gzip.
"""),
cfg.IntOpt('zvm_reachable_timeout',
               default=300,
               help="""
Timeout (seconds) to wait for an instance to start.

The z/VM driver relies on SSH between the instance and xCAT for communication.
So after an instance is logged on, it must have enough time to start SSH
communication. The driver will keep rechecking SSH communication to the
instance for this timeout. If it can not SSH to the instance, it will notify
the user that starting the instance failed and put the instance in ERROR state.
The underlying z/VM guest will then be deleted.

Possible Values:
    Any positive integer. Recommended to be at least 300 seconds (5 minutes),
    but it will vary depending on instance and system load.
    A value of 0 is used for debug. In this case the underlying z/VM guest
    will not be deleted when the instance is marked in ERROR state.
"""),
    ]

CONF = cfg.CONF
CONF.register_opts(zvm_opts)
CONF.register_opts(zvm_user_opts)
CONF.register_opts(zvm_image_opts)
