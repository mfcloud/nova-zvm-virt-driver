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

import copy

import eventlet
import mock

from nova.compute import power_state
from nova import context
from nova import exception as nova_exception
from nova.image import api as image_api
from nova.network import model as network_model
from nova import objects
from nova.objects import fields as obj_fields
from nova import test
from nova.tests.unit import fake_instance
from nova.tests import uuidsentinel
from nova.virt import fake

from nova_zvm.virt.zvm import conf
from nova_zvm.virt.zvm import const
from nova_zvm.virt.zvm import driver
from nova_zvm.virt.zvm import exception
from nova_zvm.virt.zvm import utils as zvmutils


CONF = conf.CONF
CONF.import_opt('host', 'nova.conf')
CONF.import_opt('my_ip', 'nova.conf')


class ZVMDriverTestCases(test.NoDBTestCase):
    """Unit tests for z/VM driver methods."""

    @mock.patch.object(driver.ZVMDriver, 'update_host_status')
    def setUp(self, update_host_status):
        super(ZVMDriverTestCases, self).setUp()

        self.flags(host='fakehost',
                   my_ip='10.1.1.10',
                   instance_name_template='test%04x')
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
        self._context = context.RequestContext('fake_user', 'fake_project')
        self._uuid = uuidsentinel.foo
        self._image_id = uuidsentinel.foo
        self._instance_values = {
            'display_name': 'test',
            'uuid': self._uuid,
            'vcpus': 1,
            'memory_mb': 1024,
            'image_ref': self._image_id,
            'root_gb': 0,
        }
        self._instance = fake_instance.fake_instance_obj(
                                self._context, **self._instance_values)
        self._flavor = objects.Flavor(name='testflavor', memory_mb=512,
                                      vcpus=1, root_gb=3, ephemeral_gb=10,
                                      swap=0, extra_specs={})
        self._instance.flavor = self._flavor

        self._eph_disks = [{'guest_format': u'ext3',
                      'device_name': u'/dev/sdb',
                      'disk_bus': None,
                      'device_type': None,
                      'size': 1},
                     {'guest_format': u'ext4',
                      'device_name': u'/dev/sdc',
                      'disk_bus': None,
                      'device_type': None,
                      'size': 2}]
        self._block_device_info = {'swap': None,
                                   'root_device_name': u'/dev/sda',
                                   'ephemerals': self._eph_disks,
                                   'block_device_mapping': []}

        fake_image_meta = {'status': 'active',
                                 'properties': {'os_distro': 'rhel7.2'},
                                 'name': 'rhel72eckdimage',
                                 'deleted': False,
                                 'container_format': 'bare',
                                 'disk_format': 'raw',
                                 'id': self._image_id,
                                 'owner': 'cfc26f9d6af948018621ab00a1675310',
                                 'checksum': 'b026cd083ef8e9610a29eaf71459cc',
                                 'min_disk': 0,
                                 'is_public': False,
                                 'deleted_at': None,
                                 'min_ram': 0,
                                 'size': 465448142}
        self._image_meta = objects.ImageMeta.from_dict(fake_image_meta)
        subnet_4 = network_model.Subnet(cidr='192.168.0.1/24',
                                        dns=[network_model.IP('192.168.0.1')],
                                        gateway=
                                            network_model.IP('192.168.0.1'),
                                        ips=[
                                            network_model.IP('192.168.0.100')],
                                        routes=None)
        network = network_model.Network(id=0,
                                        bridge='fa0',
                                        label='fake',
                                        subnets=[subnet_4],
                                        vlan=None,
                                        bridge_interface=None,
                                        injected=True)
        self._network_values = {
            'id': None,
            'address': 'DE:AD:BE:EF:00:00',
            'network': network,
            'type': network_model.VIF_TYPE_OVS,
            'devname': None,
            'ovs_interfaceid': None,
            'rxtx_cap': 3
        }
        self._network_info = network_model.NetworkInfo([
                network_model.VIF(**self._network_values)
        ])

    def test_init_driver(self):
        self.assertIsInstance(self.driver._sdkreq,
                              zvmutils.zVMSDKRequestHandler)
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

    @mock.patch.object(zvmutils.zVMSDKRequestHandler, 'call')
    def test_update_host_status(self, host_get_info):
        host_get_info.return_value = {
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
        host_get_info.assert_called_with('host_get_info')
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
                                     obj_fields.VMMode.HVM)],
            'ipl_time': 'IPL at 03/13/14 21:43:12 EDT',
            }]
        res = self.driver.get_available_resource('fakenode')
        self.assertEqual(res['vcpus'], 10)
        self.assertEqual(res['memory_mb_used'], 765432)
        self.assertEqual(res['disk_available_least'], 38842)

    @mock.patch.object(zvmutils.zVMSDKRequestHandler, 'call')
    def test_list_instances(self, guest_list):
        self.driver.list_instances()
        guest_list.assert_called_once_with('guest_list')

    @mock.patch.object(zvmutils.zVMSDKRequestHandler, 'call')
    def test_get_info(self, get_power_state):
        get_power_state.return_value = 'on'
        fake_inst = fake_instance.fake_instance_obj(self._context,
                    name='fake', power_state=power_state.RUNNING,
                    memory_mb='1024',
                    vcpus='4')
        inst_info = self.driver.get_info(fake_inst)
        get_power_state.assert_called_once_with('guest_get_power_state',
                                                fake_inst['name'])
        self.assertEqual(inst_info.state, 0x01)

    @mock.patch.object(zvmutils.zVMSDKRequestHandler, 'call')
    def test_get_info_off(self, gps):
        gps.return_value = 'off'
        fake_inst = fake_instance.fake_instance_obj(self._context,
                    name='fake', power_state=power_state.SHUTDOWN,
                    memory_mb='1024',
                    vcpus='4')
        inst_info = self.driver.get_info(fake_inst)
        self.assertEqual(inst_info.state, power_state.SHUTDOWN)

    @mock.patch.object(zvmutils.zVMSDKRequestHandler, 'call')
    def test_get_info_instance_not_exist_error(self, get_power_state):
        get_power_state.side_effect = exception.ZVMSDKRequestFailed(msg='err',
                                                    results={'overallRC': 404})
        fake_inst = fake_instance.fake_instance_obj(self._context,
                    name='fake', power_state=power_state.RUNNING,
                    memory_mb='1024',
                    vcpus='4')
        self.assertRaises(nova_exception.InstanceNotFound,
                          self.driver.get_info,
                          fake_inst)

    def test_get_available_nodes(self):
        nodes = self.driver.get_available_nodes()
        self.assertEqual(nodes[0], 'fakenode')

    @mock.patch.object(zvmutils.zVMSDKRequestHandler, 'call')
    def test_get_console_output(self, gco):
        self.driver.get_console_output({}, self._instance)
        gco.assert_called_with('guest_get_console_output', 'test0001')

    @mock.patch.object(driver.ZVMDriver, '_wait_network_ready')
    @mock.patch.object(driver.ZVMDriver, '_setup_network')
    @mock.patch.object(zvmutils.ImageUtils, 'import_spawn_image')
    @mock.patch.object(zvmutils.zVMSDKRequestHandler, 'call')
    @mock.patch.object(zvmutils.VMUtils, 'generate_configdrive')
    def _test_spawn(self, gen_conf_file, sdk_req, imp_img, set_net, wait_net,
                   has_import_image=False, has_get_root_units=False,
                   has_eph_disks=False):
        disk_list = [{'is_boot_disk': True, 'size': '3g'}]
        eph_disk_list = [{'format': u'ext3', 'size': '1g'},
                         {'format': u'ext4', 'size': '2g'}]
        _inst = copy.copy(self._instance)
        _bdi = copy.copy(self._block_device_info)

        sdk_req_resp = []
        # image query return '' if has step of import image
        if has_import_image:
            sdk_req_resp.append(exception.ZVMSDKRequestFailed(msg='testerr',
                                                results={'overallRC': 404}))
        else:
            sdk_req_resp.append([[self._image_meta.id]])
        # image query again
        sdk_req_resp.append([[self._image_meta.id]])
        # get root disk units
        if has_get_root_units:
            # overwrite
            disk_list = [{'is_boot_disk': True, 'size': '3338'}]
            sdk_req_resp.append('3338')
            _inst['root_gb'] = 0
        else:
            _inst['root_gb'] = 3
        # guest_create and guest_deploy
        sdk_req_resp += ['', '', '']
        # configure eph disks
        if has_eph_disks:
            sdk_req_resp.append('')
            disk_list += eph_disk_list
        else:
            _bdi['ephemerals'] = []
        sdk_req.side_effect = sdk_req_resp

        self.driver.spawn(self._context, _inst, self._image_meta,
                          injected_files=None, admin_password=None,
                          allocations=None, network_info=self._network_info,
                          block_device_info=_bdi, flavor=self._flavor)
        gen_conf_file.assert_called_once_with(self._context, _inst,
                                              None, None)
        sdk_req.assert_any_call('image_query', self._image_meta.id)
        if has_get_root_units:
            sdk_req.assert_any_call('image_get_root_disk_size',
                                    self._image_meta.id)
        sdk_req.assert_any_call('guest_create', _inst['name'],
                                1, 1024, disk_list)
        if has_eph_disks:
            sdk_req.assert_any_call('guest_config_minidisks',
                                    _inst['name'], eph_disk_list)
        sdk_req.assert_any_call('guest_start', _inst['name'])
        if has_import_image:
            imp_img.assert_called_once_with(self._context, self._image_meta.id,
                                        self._image_meta.properties.os_distro)
        set_net.assert_called_once_with(_inst['name'],
                                        self._image_meta.properties.os_distro,
                                        self._network_info)
        wait_net.assert_called_once_with(_inst)

    def test_spawn_invalid_userid(self):
        self.flags(instance_name_template='test%05x')
        self.addCleanup(self.flags, instance_name_template='test%04x')
        invalid_inst = fake_instance.fake_instance_obj(self._context,
                                                       name='123456789')
        self.assertRaises(nova_exception.InvalidInput, self.driver.spawn,
                          self._context, invalid_inst, self._image_meta,
                          injected_files=None, admin_password=None,
                          allocations=None, network_info=self._network_info,
                          block_device_info=self._block_device_info,
                          flavor=self._flavor)

    def test_spawn_simple_path(self):
        self._test_spawn()

    def test_spawn_with_eph_disks(self):
        self._test_spawn(has_eph_disks=True)

    def test_spawn_with_import_image(self):
        self._test_spawn(has_import_image=True)

    def test_spawn_with_get_root_units(self):
        self._test_spawn(has_get_root_units=True)

    @mock.patch.object(driver.ZVMDriver, '_instance_exists')
    @mock.patch.object(zvmutils.zVMSDKRequestHandler, 'call')
    def test_destroy(self, guest_delete, instance_exists):
        instance_exists.return_value = True
        self.driver.destroy(self._context, self._instance,
                            network_info=self._network_info)
        guest_delete.assert_called_once_with('guest_delete',
                                             self._instance['name'])

    @mock.patch.object(driver.ZVMDriver, '_instance_exists')
    @mock.patch.object(zvmutils.zVMSDKRequestHandler, 'call')
    def test_power_off(self, guest_stop, instance_exists):
        instance_exists.return_value = True
        self.driver.power_off(self._instance)
        guest_stop.assert_called_once_with('guest_stop',
                                           self._instance['name'], 0, 0)

    @mock.patch.object(driver.ZVMDriver, '_instance_exists')
    @mock.patch.object(zvmutils.zVMSDKRequestHandler, 'call')
    def test_power_on(self, guest_start, instance_exists):
        instance_exists.return_value = True
        self.driver.power_on(self._context, self._instance,
                             network_info=self._network_info)
        guest_start.assert_called_once_with('guest_start',
                                            self._instance['name'])
