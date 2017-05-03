# Copyright 2017 IBM Corp.
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


"""Test suite for ZVMDriver."""

import eventlet
import mock

from nova import context
from nova.image import api as image_api
from nova import test
from nova.virt import fake
from nova.virt.zvm import conf
from nova.virt.zvm import driver
from nova.virt.zvm import utils as zvmutils
from zvmsdk import api as sdkapi


CONF = conf.CONF
CONF.import_opt('host', 'nova.conf')
CONF.import_opt('my_ip', 'nova.conf')


class ZVMDriverTestCases(test.NoDBTestCase):
    """Unit tests for z/VM driver methods."""

    @mock.patch.object(driver.ZVMDriver, 'update_host_status')
    def setUp(self, update_host_status):
        super(ZVMDriverTestCases, self).setUp()
        self.context = context.get_admin_context()
        self.flags(host='fakehost',
                   my_ip='10.1.1.10')
        update_host_status.return_value = [{
            'host': 'fakehost',
            'allowed_vm_type': 'zLinux',
            'vcpus': 10,
            'vcpus_used': 10,
            'cpu_info': {'Architecture': 's390x', 'CEC model': '2097'},
            'disk_total': 406105,
            'disk_used': 367263,
            'disk_available': 38842,
            'host_memory_total': 16384,
            'host_memory_free': 8096,
            'hypervisor_type': 'zvm',
            'hypervisor_version': '630',
            'hypervisor_hostname': 'fakenode',
            'supported_instances': [('s390x', 'zvm', 'hvm')],
            'ipl_time': 'IPL at 03/13/14 21:43:12 EDT',
        }]
        self.driver = driver.ZVMDriver(fake.FakeVirtAPI())

    def test_init_driver(self):
        self.assertIsInstance(self.driver._sdk_api, sdkapi.SDKAPI)
        self.assertIsInstance(self.driver._vmutils, zvmutils.VMUtils)
        self.assertIsInstance(self.driver._image_api, image_api.API)
        self.assertIsInstance(self.driver._pathutils, zvmutils.PathUtils)
        self.assertIsInstance(self.driver._imageutils, zvmutils.ImageUtils)
        self.assertIsInstance(self.driver._networkutils,
                              zvmutils.NetworkUtils)
        self.assertIsInstance(self.driver._imageop_semaphore,
                              eventlet.semaphore.Semaphore)
        self.assertEqual(self.driver._host_stats[0]['host'], "fakehost")
        self.assertEqual(self.driver._host_stats[0]['disk_available'], 38842)

    @mock.patch.object(sdkapi.SDKAPI, 'get_host_info')
    def test_update_host_status(self, get_host_info):
        get_host_info.return_value = {
            'vcpus': 10,
            'vcpus_used': 10,
            'cpu_info': {'Architecture': 's390x', 'CEC model': '2097'},
            'disk_total': 406105,
            'disk_used': 367263,
            'disk_available': 38842,
            'memory_mb': 876543,
            'memory_mb_used': 111111,
            'hypervisor_type': 'zvm',
            'hypervisor_version': '630',
            'hypervisor_hostname': 'fakenode',
            'ipl_time': 'IPL at 03/13/14 21:43:12 EDT',
            }
        info = self.driver.update_host_status()
        get_host_info.assert_called_with()
        self.assertEqual(info[0]['host'], CONF.host)
        self.assertEqual(info[0]['hypervisor_hostname'], 'fakenode')
        self.assertEqual(info[0]['host_memory_free'], 765432)

    @mock.patch.object(sdkapi.SDKAPI, 'list_vms')
    def test_list_instances(self, list_vms):
        self.driver.list_instances()
        list_vms.assert_called_once_with()
