# Copyright 2015 Cisco Systems, Inc.  All rights reserved.
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

from functools import wraps
import time

from oslo_log import log as logging
from sqlalchemy.sql import expression as expr

from neutron.api.v2 import attributes
from neutron.common import exceptions as n_exc
from neutron.db import models_v2
from neutron.i18n import _LE, _LI, _LW
from neutron import manager
from neutron.plugins.cisco.device_manager import plugging_drivers
from neutron.plugins.common import constants as svc_constants

LOG = logging.getLogger(__name__)


DELETION_ATTEMPTS = 4
SECONDS_BETWEEN_DELETION_ATTEMPTS = 3


class VIFHotPlugPluggingDriver(plugging_drivers.PluginSidePluggingDriver):
    """Driver class for service VMs used with Neutron plugins supporting
    VIF hotplug.
    """

    def retry(ExceptionToCheck, tries=4, delay=3, backoff=2):
        """Retry calling the decorated function using an exponential backoff.

        Reference: http://www.saltycrane.com/blog/2009/11/trying-out-retry
        -decorator-python/

        :param ExceptionToCheck: the exception to check. may be a tuple of
            exceptions to check
        :param tries: number of times to try (not retry) before giving up
        :param delay: initial delay between retries in seconds
        :param backoff: backoff multiplier e.g. value of 2 will double the
                        delay each retry
        """

        def deco_retry(f):
            @wraps(f)
            def f_retry(*args, **kwargs):
                mtries, mdelay = tries, delay
                while mtries > 1:
                    try:
                        return f(*args, **kwargs)
                    except ExceptionToCheck as e:
                        LOG.error(_LE('%(ex)s. Retrying in %(delay)d '
                                      'seconds.'),
                                  {'ex': str(e), 'delay': mdelay})
                        time.sleep(mdelay)
                        mtries -= 1
                        mdelay *= backoff
                return f(*args, **kwargs)

            return f_retry  # true decorator

        return deco_retry

    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

    @property
    def _dev_mgr(self):
        return manager.NeutronManager.get_service_plugins().get(
            svc_constants.DEVICE_MANAGER)

    def create_hosting_device_resources(self, context, complementary_id,
                                        tenant_id, mgmt_context, max_hosted):
        """Create resources for a hosting device in a plugin specific way."""
        mgmt_port = None
        if mgmt_context and mgmt_context.get('mgmt_nw_id') and tenant_id:
            # Create port for mgmt interface
            p_spec = {'port': {
                'tenant_id': tenant_id,
                'admin_state_up': True,
                'name': 'mgmt',
                'network_id': mgmt_context['mgmt_nw_id'],
                'mac_address': attributes.ATTR_NOT_SPECIFIED,
                'fixed_ips': attributes.ATTR_NOT_SPECIFIED,
                'device_id': "",
                # Use device_owner attribute to ensure we can query for these
                # ports even before Nova has set device_id attribute.
                'device_owner': complementary_id}}
            try:
                mgmt_port = self._core_plugin.create_port(context, p_spec)
            except n_exc.NeutronException as e:
                LOG.error(_LE('Error %s when creating management port. '
                              'Cleaning up.'), e)
                self.delete_hosting_device_resources(
                    context, tenant_id, mgmt_port)
                mgmt_port = None
        # We are setting the 'ports' to an empty list as it is expected by
        # the callee: device_handling_db._create_csr1kv_vm_hosting_device()
        return {'mgmt_port': mgmt_port, 'ports': []}

    def get_hosting_device_resources(self, context, id, complementary_id,
                                     tenant_id, mgmt_nw_id):
        """Returns information about all resources for a hosting device."""
        mgmt_port = None
        # Ports for hosting device may not yet have 'device_id' set to
        # Nova assigned uuid of VM instance. However, those ports will still
        # have 'device_owner' attribute set to complementary_id. Hence, we
        # use both attributes in the query to ensure we find all ports.
        query = context.session.query(models_v2.Port)
        query = query.filter(expr.or_(
            models_v2.Port.device_id == id,
            models_v2.Port.device_owner == complementary_id))
        for port in query:
            if port['network_id'] != mgmt_nw_id:
                LOG.debug('Ignoring hosting device port %s while assembling '
                          'resources since that port is not on the management '
                          'network', port['id'])
            else:
                mgmt_port = port
                # There should only be the management port
                break
        return {'mgmt_port': mgmt_port}

    def delete_hosting_device_resources(self, context, tenant_id, mgmt_port,
                                        **kwargs):
        """Deletes resources for a hosting device in a plugin specific way."""

        if mgmt_port is not None:
            try:
                self._delete_resource_port(context, mgmt_port['id'])
            except n_exc.NeutronException as e:
                LOG.error(_LE('Unable to delete port:%(port)s after %(tries)d '
                              'attempts due to exception %(exception)s. '
                              'Skipping it.'),
                          {'port': mgmt_port['id'], 'tries': DELETION_ATTEMPTS,
                           'exception': str(e)})

    @retry(n_exc.NeutronException, DELETION_ATTEMPTS, 1)
    def _delete_resource_port(self, context, port_id):
        try:
            self._core_plugin.delete_port(context, port_id)
            LOG.info(_LI('Port %s deleted successfully'), port_id)
        except n_exc.PortNotFound:
            LOG.warning(_LW('Trying to delete port:%s, but port is not found'),
                        port_id)

    def setup_logical_port_connectivity(self, context, port_db,
                                        hosting_device_id):
        """Establishes connectivity for a logical port.

        This is done by hot plugging the interface(VIF) of the service VM
        to a Neutron port.
        """
        l3admin_tenant_id = self._dev_mgr.l3_tenant_id()
        # Clear device_owner and device_id and set tenant_id to L3AdminTenant
        # to let interface-attach succeed
        self._core_plugin.update_port(
            context.elevated(),
            port_db['id'],
            {'port': {'device_owner': '',
                      'device_id': '',
                      'tenant_id': l3admin_tenant_id}})
        try:
            self._dev_mgr.svc_vm_mgr.interface_attach(hosting_device_id,
                                                      port_db['id'])
            LOG.debug('Setup logical port completed for port:%s',
                      port_db['id'])
        except Exception as e:
            LOG.error(_LE("Failed to attach interface corresponding to port:"
                          "%(p_id)s on hosting device:%(hd_id)s due to "
                          "error %(str(e))"), {'p_id': port_db['id'],
                                               'hd_id': hosting_device_id,
                                               'error': e})

    def teardown_logical_port_connectivity(self, context, port_db,
                                           hosting_device_id):
        """Removes connectivity for a logical port.

        This is done by hot unplugging the interface(VIF) of the
        service VM from the Neutron port it uses.
        """
        if port_db is None or port_db.get('id') is None:
            LOG.error(_LE("Port id is None! Cannot remove port "
                          "from hosting_device:%s"), hosting_device_id)
            return
        try:
            self._dev_mgr.svc_vm_mgr.interface_detach(hosting_device_id,
                                                      port_db['id'])
            LOG.debug("Done teardown logical port connectivity for port:%s",
                      port_db['id'])
        except Exception as e:
            LOG.error(_LE("Failed to detach interface corresponding to port:"
                          "%(p_id)s on hosting device:%(hd_id)s due to "
                          "error %(str(e))"), {'p_id': port_db['id'],
                                               'hd_id': hosting_device_id,
                                               'error': e})

    def extend_hosting_port_info(self, context, port_db,
                                 hosting_device, hosting_info):
        """Extends hosting information for a logical port."""
        return

    def allocate_hosting_port(self, context, router_id, port_db, network_type,
                              hosting_device_id):
        """Allocates a hosting port for a logical port."""
        return {'allocated_port_id': port_db['id'],
                'allocated_vlan': None}