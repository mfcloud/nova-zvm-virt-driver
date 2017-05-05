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

from nova.compute import power_state
from nova.compute import vm_mode
from nova import context
from nova import exception as nova_exception
from nova.image import api as image_api
from nova import test
from nova.tests.unit import fake_instance
from nova.virt import fake
from nova.virt import hardware
from nova.virt.zvm import conf
from nova.virt.zvm import const
from nova.virt.zvm import driver
from nova.virt.zvm import utils as zvmutils
from zvmsdk import api as sdkapi
from zvmsdk import exception as sdkexception


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

    @mock.patch.object(driver.ZVMDriver, 'update_host_status')
    def test_get_available_resource(self, update_host_status):
        update_host_status.return_value = [{
            'host': CONF.host,
            'allowed_vm_type': const.ALLOWED_VM_TYPE,
            'vcpus': 10,
            'vcpus_used': 10,
            'cpu_info': {'Architecture': 's390x', 'CEC model': '2097'},
            'disk_total': 406105,
            'disk_used': 367263,
            'disk_available': 38842,
            'host_memory_total': 876543,
            'host_memory_free': 111111,
            'hypervisor_type': 'zvm',
            'hypervisor_version': '630',
            'hypervisor_hostname': 'fakenode',
            'supported_instances': [(const.ARCHITECTURE,
                                     const.HYPERVISOR_TYPE,
                                     vm_mode.HVM)],
            'ipl_time': 'IPL at 03/13/14 21:43:12 EDT',
            }]
        res = self.driver.get_available_resource('fakenode')
        self.assertEqual(res['vcpus'], 10)
        self.assertEqual(res['memory_mb_used'], 765432)
        self.assertEqual(res['disk_available_least'], 38842)

    @mock.patch.object(sdkapi.SDKAPI, 'list_vms')
    def test_list_instances(self, list_vms):
        self.driver.list_instances()
        list_vms.assert_called_once_with()

    @mock.patch.object(sdkapi.SDKAPI, 'get_vm_info')
    @mock.patch.object(zvmutils, 'mapping_power_stat')
    def test_get_instance_info_paused(self, mapping_power_stat, get_vm_info):
        get_vm_info.return_value = {'power_state': 'on',
                                    'max_mem_kb': 2097152,
                                    'mem_kb': 44,
                                    'num_cpu': 2,
                                    'cpu_time_ns': 796000,
                                    }
        mapping_power_stat.return_value = power_state.RUNNING
        fake_inst = fake_instance.fake_instance_obj(self.context,
                    name='fake', power_state=power_state.PAUSED,
                    memory_mb='1024',
                    vcpus='4')
        inst_info = self.driver._get_instance_info(fake_inst)
        mapping_power_stat.assert_called_once_with('on')
        self.assertEqual(inst_info.state, power_state.PAUSED)
        self.assertEqual(inst_info.mem_kb, 44)

    @mock.patch.object(sdkapi.SDKAPI, 'get_vm_info')
    @mock.patch.object(zvmutils, 'mapping_power_stat')
    def test_get_instance_info_off(self, mapping_power_stat, get_vm_info):
        get_vm_info.return_value = {'power_state': 'off',
                                    'max_mem_kb': 2097152,
                                    'mem_kb': 44,
                                    'num_cpu': 2,
                                    'cpu_time_ns': 796000,
                                    }
        mapping_power_stat.return_value = power_state.SHUTDOWN
        fake_inst = fake_instance.fake_instance_obj(self.context,
                    name='fake', power_state=power_state.PAUSED,
                    memory_mb='1024',
                    vcpus='4')
        inst_info = self.driver._get_instance_info(fake_inst)
        mapping_power_stat.assert_called_once_with('off')
        self.assertEqual(inst_info.state, power_state.SHUTDOWN)
        self.assertEqual(inst_info.mem_kb, 44)

    @mock.patch.object(driver.ZVMDriver, '_get_instance_info')
    def test_get_info(self, _get_instance_info):
        _fake_inst_info = hardware.InstanceInfo(state=0x01, mem_kb=131072,
                            num_cpu=4, cpu_time_ns=330528353,
                            max_mem_kb=1048576)
        _get_instance_info.return_value = _fake_inst_info
        fake_inst = fake_instance.fake_instance_obj(self.context,
                    name='fake', power_state=power_state.RUNNING,
                    memory_mb='1024',
                    vcpus='4')
        inst_info = self.driver.get_info(fake_inst)
        self.assertEqual(0x01, inst_info.state)
        self.assertEqual(131072, inst_info.mem_kb)
        self.assertEqual(4, inst_info.num_cpu)
        self.assertEqual(330528353, inst_info.cpu_time_ns)
        self.assertEqual(1048576, inst_info.max_mem_kb)

    @mock.patch.object(driver.ZVMDriver, '_get_instance_info')
    def test_get_info_instance_not_exist_error(self, _get_instance_info):
        _get_instance_info.side_effect = sdkexception.ZVMVirtualMachineNotExist
        fake_inst = fake_instance.fake_instance_obj(self.context,
                    name='fake', power_state=power_state.RUNNING,
                    memory_mb='1024',
                    vcpus='4')
        self.assertRaises(nova_exception.InstanceNotFound,
                          self.driver.get_info,
                          fake_inst)

    def test_get_available_nodes(self):
        nodes = self.driver.get_available_nodes()
        self.assertEqual(nodes[0], 'fakenode')
