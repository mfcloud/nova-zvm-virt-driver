# Copyright 2013 IBM Corp.
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


import datetime
import eventlet
import six
import time

from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_service import loopingcall
from oslo_utils import excutils
from oslo_utils import timeutils

from nova.compute import power_state
from nova.compute import vm_mode
from nova import exception as nova_exception
from nova.i18n import _, _LI, _LW
from nova.image import api as image_api
from nova.objects import migrate_data as migrate_data_obj
from nova.virt import driver
from nova.virt import hardware
from nova.virt.zvm import conf
from nova.virt.zvm import const
from nova.virt.zvm import exception
from nova.virt.zvm import utils as zvmutils
from zvmsdk import api as sdkapi
from zvmsdk import exception as sdkexception


LOG = logging.getLogger(__name__)

CONF = conf.CONF
CONF.import_opt('default_ephemeral_format', 'nova.conf')
CONF.import_opt('host', 'nova.conf')
CONF.import_opt('my_ip', 'nova.conf')


class ZVMDriver(driver.ComputeDriver):
    """z/VM implementation of ComputeDriver."""

    capabilities = {
        "has_imagecache": True,
        "supports_recreate": False,
        "supports_migrate_to_same_host": True,
        "supports_attach_interface": False
    }

    def __init__(self, virtapi):
        super(ZVMDriver, self).__init__(virtapi)
        self._sdk_api = sdkapi.SDKAPI()
        self._vmutils = zvmutils.VMUtils()

        self._image_api = image_api.API()
        self._pathutils = zvmutils.PathUtils()
        self._imageutils = zvmutils.ImageUtils()
        self._networkutils = zvmutils.NetworkUtils()
        self._imageop_semaphore = eventlet.semaphore.Semaphore(1)

        # incremental sleep interval list
        _inc_slp = [5, 10, 20, 30, 60]
        _slp = 5

        self._host_stats = []
        _slp = 5

        while (self._host_stats == []):
            try:
                self._host_stats = self.update_host_status()
            except Exception as e:
                # Ignore any exceptions and log as warning
                _slp = len(_inc_slp) != 0 and _inc_slp.pop(0) or _slp
                msg = _LW("Failed to get host stats while initializing zVM "
                          "driver due to reason %(reason)s, will re-try in "
                          "%(slp)d seconds")
                LOG.warning(msg, {'reason': six.text_type(e),
                               'slp': _slp})
                time.sleep(_slp)

    def init_host(self, host):
        """Initialize anything that is necessary for the driver to function,
        including catching up with currently running VM's on the given host.
        """
        pass

    def _get_instance_info(self, instance):
        inst_name = instance['name']
        vm_info = self._sdk_api.guest_get_info(inst_name)
        _instance_info = hardware.InstanceInfo()

        power_stat = zvmutils.mapping_power_stat(vm_info['power_state'])
        if ((power_stat == power_state.RUNNING) and
            (instance['power_state'] == power_state.PAUSED)):
            # return paused state only previous power state is paused
            _instance_info.state = power_state.PAUSED
        else:
            _instance_info.state = power_stat

        _instance_info.max_mem_kb = vm_info['max_mem_kb']
        _instance_info.mem_kb = vm_info['mem_kb']
        _instance_info.num_cpu = vm_info['num_cpu']
        _instance_info.cpu_time_ns = vm_info['cpu_time_us'] * 1000

        return _instance_info

    def get_info(self, instance):
        """Get the current status of an instance, by name (not ID!)

        Returns a dict containing:
        :state:           the running state, one of the power_state codes
        :max_mem:         (int) the maximum memory in KBytes allowed
        :mem:             (int) the memory in KBytes used by the domain
        :num_cpu:         (int) the number of virtual CPUs for the domain
        :cpu_time:        (int) the CPU time used in nanoseconds

        """
        inst_name = instance['name']

        try:
            return self._get_instance_info(instance)
        except sdkexception.ZVMVirtualMachineNotExist:
            LOG.warning(_LW("z/VM instance %s does not exist") % inst_name,
                        instance=instance)
            raise nova_exception.InstanceNotFound(instance_id=inst_name)
        except Exception as err:
            # TODO(YDY): raise nova_exception.InstanceNotFound
            LOG.warning(_LW("Failed to get the info of z/VM instance %s") %
                        inst_name, instance=instance)
            raise err

    def list_instances(self):
        """Return the names of all the instances known to the virtualization
        layer, as a list.
        """
        return self._sdk_api.host_list_guests()

    def _instance_exists(self, instance_name):
        """Overwrite this to using instance name as input parameter."""
        return instance_name in self.list_instances()

    def instance_exists(self, instance):
        """Overwrite this to using instance name as input parameter."""
        return self._instance_exists(instance.name)

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None,
              flavor=None):
        LOG.info(_LI("Spawning new instance %s on zVM hypervisor") %
                 instance['name'], instance=instance)
        # For zVM instance, limit the maximum length of instance name to \ 8
        if len(instance['name']) > 8:
            msg = (_("Don't support spawn vm on zVM hypervisor with instance "
                "name: %s, please change your instance_name_template to make "
                "sure the length of instance name is not longer than 8 "
                "characters") % instance['name'])
            raise nova_exception.InvalidInput(reason=msg)
        try:
            spawn_start = time.time()
            image_meta = self._image_api.get(context, image_meta.id)
            os_version = image_meta['properties']['os_version']

            # TODO(YaLian) will remove network files from this
            transportfiles = self._vmutils.generate_configdrive(
                            context, instance, os_version, network_info,
                            injected_files, admin_password)

            with self._imageop_semaphore:
                spawn_image_exist = self._sdk_api.image_query(
                                    image_meta['id'])
                if not spawn_image_exist:
                    self._imageutils.import_spawn_image(
                        context, image_meta['id'], os_version)

            spawn_image_name = self._sdk_api.image_query(
                                    image_meta['id'])

            if instance['root_gb'] == 0:
                root_disk_size = self._sdk_api.get_image_root_disk_size(
                                                spawn_image_name)
            else:
                root_disk_size = '%ig' % instance['root_gb']

            disk_list = []
            root_disk = {'size': root_disk_size,
                         'is_boot_disk': True
                         }
            disk_list.append(root_disk)
            ephemeral_disks_info = block_device_info.get('ephemerals', [])
            eph_list = []
            for eph in ephemeral_disks_info:
                eph_dict = {'size': eph['size'],
                            'format': (eph['guest_format'] or
                                       CONF.default_ephemeral_format)}
                eph_list.append(eph_dict)

            if eph_list:
                disk_list.extend(eph_list)
            self._sdk_api.guest_create(instance['name'], instance['vcpus'],
                                       instance['memory_mb'], disk_list)

            # Setup network for z/VM instance
            self._setup_network(instance['name'], network_info)
            self._sdk_api.deploy_image_to_vm(instance['name'],
                                             spawn_image_name,
                                             transportfiles)

            # Handle ephemeral disks
            if eph_list:
                self._sdk_api.process_addtional_disks(instance['name'],
                                                      eph_list)

            self._wait_network_ready(instance['name'], instance)

            self._sdk_api.power_on(instance['name'])
            spawn_time = time.time() - spawn_start
            LOG.info(_LI("Instance spawned succeeded in %s seconds") %
                     spawn_time, instance=instance)
        except Exception as err:
            with excutils.save_and_reraise_exception():
                LOG.error(_("Deploy image to instance %(instance)s "
                            "failed with reason: %(err)s") %
                          {'instance': instance['name'], 'err': err},
                          instance=instance)
                self.destroy(context, instance, network_info,
                             block_device_info)

    def _setup_network(self, vm_name, network_info):
        network = network_info[0]['network']
        ip_addr = network['subnets'][0]['ips'][0]['address']

        LOG.debug("Creating NICs for vm %s", vm_name)
        nic_list = []
        for vif in network_info:
            nic_dict = {'nic_id': vif['id'],
                        'mac_addr': vif['address']}
            nic_list.append(nic_dict)
        self._sdk_api.guest_create_nic(vm_name, nic_list, ip_addr)

    def _wait_network_ready(self, inst_name, instance):
        """Wait until neutron zvm-agent add all NICs to vm"""
        def _wait_for_nics_add_in_vm(inst_name, expiration):
            if (CONF.zvm_reachable_timeout and
                    timeutils.utcnow() > expiration):
                msg = _("NIC update check failed "
                        "on instance:%s") % instance.uuid
                raise exception.ZVMNetworkError(msg=msg)

            try:
                switch_dict = self._sdk_api.guest_get_nic_switch_info(
                                                inst_name)
                if switch_dict and '' not in switch_dict.values():
                    for key in switch_dict:
                        result = self._sdk_api.guest_get_definition_info(
                                                inst_name, nic_coupled=key)
                        if not result['nic_coupled']:
                            return
                else:
                    # In this case, the nic switch info is not ready yet
                    # need another loop to check until time out or find it
                    return

            except exception.ZVMBaseException as e:
                # Ignore any zvm driver exceptions
                LOG.info(_LI('encounter error %s during get vswitch info'),
                         e.format_message(), instance=instance)
                return

            # Enter here means all NIC granted
            LOG.info(_LI("All NICs are added in user direct for "
                         "instance %s."), inst_name, instance=instance)
            raise loopingcall.LoopingCallDone()

        expiration = timeutils.utcnow() + datetime.timedelta(
                             seconds=CONF.zvm_reachable_timeout)
        LOG.info(_LI("Wait neturon-zvm-agent to add NICs to %s user direct."),
                 inst_name, instance=instance)
        timer = loopingcall.FixedIntervalLoopingCall(
                    _wait_for_nics_add_in_vm, inst_name, expiration)
        timer.start(interval=10).wait()

    @property
    def need_legacy_block_device_info(self):
        return False

    def destroy(self, context, instance, network_info=None,
                block_device_info=None, destroy_disks=False):

        inst_name = instance['name']
        if self._instance_exists(inst_name):
            LOG.info(_LI("Destroying instance %s"), inst_name,
                     instance=instance)
            self._sdk_api.guest_delete(inst_name)
        else:
            LOG.warning(_LW('Instance %s does not exist'), inst_name,
                        instance=instance)

    def manage_image_cache(self, context, filtered_instances):
        """Clean the image cache in xCAT MN."""
        pass

    def reboot(self, context, instance, network_info, reboot_type,
               block_device_info=None, bad_volumes_callback=None):
        pass

    def attach_volume(self, context, connection_info, instance, mountpoint,
                      disk_bus=None, device_type=None, encryption=None):
        """Attach the disk to the instance at mountpoint using info."""
        pass

    def detach_volume(self, connection_info, instance, mountpoint=None,
                      encryption=None):
        """Detach the disk attached to the instance."""
        pass

    def snapshot(self, context, instance, image_href, update_task_state):
        pass

    def pause(self, instance):
        """Pause the specified instance."""
        pass

    def unpause(self, instance):
        """Unpause paused VM instance."""
        pass

    def power_off(self, instance, timeout=0, retry_interval=0):
        """Power off the specified instance."""
        pass

    def power_on(self, context, instance, network_info,
                 block_device_info=None):
        """Power on the specified instance."""
        pass

    def get_available_resource(self, nodename=None):
        """Retrieve resource information.

        This method is called when nova-compute launches, and
        as part of a periodic task

        :param nodename:
            node which the caller want to get resources from
            a driver that manages only one node can safely ignore this
        :returns: Dictionary describing resources

        """
        LOG.debug("Getting available resource for %s" % CONF.host)
        stats = self.update_host_status()[0]

        mem_used = stats['host_memory_total'] - stats['host_memory_free']
        supported_instances = stats['supported_instances']
        dic = {
            'vcpus': stats['vcpus'],
            'memory_mb': stats['host_memory_total'],
            'local_gb': stats['disk_total'],
            'vcpus_used': stats['vcpus_used'],
            'memory_mb_used': mem_used,
            'local_gb_used': stats['disk_used'],
            'hypervisor_type': stats['hypervisor_type'],
            'hypervisor_version': stats['hypervisor_version'],
            'hypervisor_hostname': stats['hypervisor_hostname'],
            'cpu_info': jsonutils.dumps(stats['cpu_info']),
            'disk_available_least': stats['disk_available'],
            'supported_instances': supported_instances,
            'numa_topology': None,
        }

        return dic

    def check_can_live_migrate_destination(self, ctxt, instance_ref,
                                           src_compute_info, dst_compute_info,
                                           block_migration=False,
                                           disk_over_commit=False):
        return migrate_data_obj.LibvirtLiveMigrateData()

    def check_can_live_migrate_source(self, ctxt, instance_ref,
                                      dest_check_data, block_device_info=None):
        return migrate_data_obj.LibvirtLiveMigrateData()

    def cleanup_live_migration_destination_check(self, ctxt,
                                                   dest_check_data):
        # For z/VM, nothing needed to be cleanup
        return

    def pre_live_migration(self, ctxt, instance_ref, block_device_info,
                           network_info, disk_info, migrate_data=None):
        pass

    def pre_block_migration(self, ctxt, instance_ref, disk_info):
        # We don't support block_migration
        return

    def live_migration(self, ctxt, instance_ref, dest,
                       post_method, recover_method, block_migration=False,
                       migrate_data=None):
        pass

    def post_live_migration_at_destination(self, ctxt, instance_ref,
                                           network_info,
                                           block_migration=False,
                                           block_device_info=None):
        pass

    def unfilter_instance(self, instance, network_info):
        return

    def reset_network(self, instance):
        pass

    def inject_network_info(self, instance, nw_info):
        pass

    def plug_vifs(self, instance, network_info):
        pass

    def unplug_vifs(self, instance, network_info):
        pass

    def ensure_filtering_rules_for_instance(self, instance_ref, network_info):
        # It enforces security groups on host initialization and live
        # migration. In z/VM we do not assume instances running upon host
        # initialization
        return

    def update_host_status(self):
        """Refresh host stats. One compute service entry possibly
        manages several hypervisors, so will return a list of host
        status information.
        """
        LOG.debug("Updating host status for %s" % CONF.host)

        caps = []

        info = self._sdk_api.host_get_info()

        data = {'host': CONF.host,
                'allowed_vm_type': const.ALLOWED_VM_TYPE}
        data['vcpus'] = info['vcpus']
        data['vcpus_used'] = info['vcpus_used']
        data['cpu_info'] = info['cpu_info']
        data['disk_total'] = info['disk_total']
        data['disk_used'] = info['disk_used']
        data['disk_available'] = info['disk_available']
        data['host_memory_total'] = info['memory_mb']
        data['host_memory_free'] = (info['memory_mb'] -
                                    info['memory_mb_used'])
        data['hypervisor_type'] = info['hypervisor_type']
        data['hypervisor_version'] = info['hypervisor_version']
        data['hypervisor_hostname'] = info['hypervisor_hostname']
        data['supported_instances'] = [(const.ARCHITECTURE,
                                        const.HYPERVISOR_TYPE,
                                        vm_mode.HVM)]
        data['ipl_time'] = info['ipl_time']

        caps.append(data)

        return caps

    def get_volume_connector(self, instance):
        pass

    def migrate_disk_and_power_off(self, context, instance, dest,
                                   instance_type, network_info,
                                   block_device_info=None,
                                   timeout=0, retry_interval=0):
        pass

    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance,
                         block_device_info=None, power_on=True):
        pass

    def confirm_migration(self, migration, instance, network_info):
        pass

    def finish_revert_migration(self, context, instance, network_info,
                                block_device_info=None, power_on=True):
        pass

    def set_admin_password(self, instance, new_pass=None):
        pass

    def get_console_output(self, context, instance):
        return self._sdk_api.guest_get_console_output(instance.name)

    def get_host_uptime(self):
        with zvmutils.expect_invalid_xcat_resp_data(self._host_stats):
            return self._host_stats[0]['ipl_time']

    def get_available_nodes(self, refresh=False):
        return [d['hypervisor_hostname'] for d in self._host_stats
                if (d.get('hypervisor_hostname') is not None)]
