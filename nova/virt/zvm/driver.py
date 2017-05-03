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


import eventlet
import six
import time

from oslo_log import log as logging
from oslo_utils import excutils

from nova.compute import vm_mode
from nova.i18n import _, _LI, _LW
from nova.image import api as image_api
from nova.objects import migrate_data as migrate_data_obj
from nova.virt import driver
from nova.virt.zvm import conf
from nova.virt.zvm import const
from nova.virt.zvm import exception
from nova.virt.zvm import utils as zvmutils
from zvmsdk import api as sdkapi


LOG = logging.getLogger(__name__)

CONF = conf.CONF
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

    def get_info(self, instance):
        """Get the current status of an instance, by name (not ID!)

        Returns a dict containing:
        :state:           the running state, one of the power_state codes
        :max_mem:         (int) the maximum memory in KBytes allowed
        :mem:             (int) the memory in KBytes used by the domain
        :num_cpu:         (int) the number of virtual CPUs for the domain
        :cpu_time:        (int) the CPU time used in nanoseconds

        """
        pass

    def list_instances(self):
        """Return the names of all the instances known to the virtualization
        layer, as a list.
        """
        return self._sdk_api.list_vms()

    def _instance_exists(self, instance_name):
        """Overwrite this to using instance name as input parameter."""
        return instance_name in self.list_instances()

    def instance_exists(self, instance):
        """Overwrite this to using instance name as input parameter."""
        return self._instance_exists(instance.name)

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None,
              flavor=None):
        try:
            LOG.info(_LI("Spawning new instance %(node) on zVM hypervisor") %
                     instance['name'], instance=instance)
            # Validate VM ID on zVM hypervisor
            self._sdk_api.validate_vm_id(instance['name'])
            spawn_start = time.time()
            image_meta = self._image_api.get(context, image_meta.id)
            os_version = image_meta['properties']['os_version']
            linuxdist = self._dist_manager.get_linux_dist(os_version)()

            # TODO(YaLian) will remove network files from this
            transportfiles = self._vmutils.generate_configdrive(
                            context, instance, os_version, network_info,
                            injected_files, admin_password, linuxdist)

            with self._imageop_semaphore:
                spawn_image_exist = self._sdk_api.check_image_exist(
                                    image_meta['id'])
                if not spawn_image_exist:
                    self._imageutils.prepare_spawn_image(
                        context, image_meta['id'], os_version)

            spawn_image_name = self._sdk_api.get_image_name(
                                    image_meta['id'])
            eph_disks = block_device_info.get('ephemerals', [])
            self._sdk_api.create_vm(instance['name'], instance['vcpus'],
                                    instance['memory_mb'],
                                    instance['root_gb'],
                                    eph_disks, spawn_image_name)

            # Setup network for z/VM instance
            self._preset_instance_network(instance['name'], network_info)
            self._add_nic_to_table(instance['name'], network_info)

            self._sdk_api.deploy_image_to_vm(spawn_image_name, transportfiles)

            # Handle ephemeral disk
            if instance['ephemeral_gb'] != 0:
                eph_disks = block_device_info.get('ephemerals', [])
                self._sdk_api.process_addtional_disks(eph_disks)

                # TODO(YaLian): Move this to utils.py
                self._wait_and_get_nic_direct(instance['name'], instance)

                self._compute_api.power_on(instance['name'])
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

    def _preset_instance_network(self, instance_name, network_info):
        self.config_xcat_mac(instance_name)
        LOG.debug("Add ip/host name on xCAT MN for instance %s" %
                  instance_name)
        try:
            network = network_info[0]['network']
            ip_addr = network['subnets'][0]['ips'][0]['address']
        except Exception:
            if network_info:
                msg = _("Invalid network info: %s") % str(network_info)
            else:
                msg = _("Network info is Empty")
            raise exception.ZVMNetworkError(msg=msg)

        self.add_xcat_host(instance_name, ip_addr, instance_name)
        self.makehosts()

    def _add_nic_to_table(self, inst_name, network_info):
        nic_vdev = CONF.zvm_default_nic_vdev
        # TODO(nafei) how to handle zhcpnode, as parameter?
        zhcpnode = self._get_hcp_info()['nodename']
        for vif in network_info:
            LOG.debug('Create xcat table value about nic: '
                      'ID is %(id)s, address is %(address)s, '
                      'vdev is %(vdev)s' %
                      {'id': vif['id'], 'address': vif['address'],
                       'vdev': nic_vdev})
            self.create_xcat_table_about_nic(zhcpnode,
                                             inst_name,
                                             vif['id'],
                                             vif['address'],
                                             nic_vdev)
            nic_vdev = str(hex(int(nic_vdev, 16) + 3))[2:]

    @property
    def need_legacy_block_device_info(self):
        return False

    def destroy(self, context, instance, network_info=None,
                block_device_info=None, destroy_disks=False):
        pass

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
        pass

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

        info = self._sdk_api.get_host_info()

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
        pass

    def get_host_uptime(self):
        with zvmutils.expect_invalid_xcat_resp_data(self._host_stats):
            return self._host_stats[0]['ipl_time']

    def get_available_nodes(self, refresh=False):
        pass
