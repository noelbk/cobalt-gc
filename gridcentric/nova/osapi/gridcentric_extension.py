# Copyright 2011 GridCentric Inc.
# All Rights Reserved.
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

import json
import webob
from webob import exc

from nova import log as logging
from nova import quota
from nova import exception as novaexc

from nova.api.openstack import create_instance_helper as server_helper
from nova.api.openstack import extensions
import nova.api.openstack.views.addresses
import nova.api.openstack.views.flavors
import nova.api.openstack.views.images
import nova.api.openstack.views.servers
import nova.api.openstack.common as common

from gridcentric.nova.api import API

LOG = logging.getLogger("nova.api.extensions.gridcentric")

def convert_exception(action):

    def fn(self, *args, **kwargs):
        try:
            return action(self, *args, **kwargs)
        except novaexc.Error as error:
            raise exc.HTTPBadRequest(explanation=unicode(error))
    # note(dscannell): Openstack sometimes does matching on the function name so we need to
    # ensure that the decorated function returns with the same function name as the action.
    fn.__name__ == action.__name__
    return fn

class Gridcentric_extension(object):
    """
    The Openstack Extension definition for the GridCentric capabilities. Currently this includes:

        * Bless an existing virtual machine (creates a new server snapshot
          of the virtual machine and enables the user to launch new copies
          nearly instantaneously).

        * Launch new virtual machines from a blessed copy above.

        * Discard blessed VMs.

        * List launched VMs (per blessed VM).
    """

    def __init__(self):
        self.gridcentric_api = API()
        # This is used to convert exception to consistent HTTP errors
        self.server_helper = server_helper.CreateInstanceHelper(None)

        # Add the gridcentric-specific states to the state map
        common._STATE_MAP['blessed'] = {'default': 'BLESSED'}

    def get_name(self):
        return "GridCentric"

    def get_alias(self):
        return "GC"

    def get_description(self):
        return "The GridCentric extension"

    def get_namespace(self):
        return "http://www.gridcentric.com"

    def get_updated(self):
        return '2012-03-14T18:33:34-07:00' ##TIMESTAMP##

    def get_actions(self):
        actions = []

        actions.append(extensions.ActionExtension('servers', 'gc_bless',
                                                    self._bless_instance))

        actions.append(extensions.ActionExtension('servers', 'gc_launch',
                                                    self._launch_instance))

        actions.append(extensions.ActionExtension('servers', 'gc_migrate',
                                                    self._migrate_instance))

        actions.append(extensions.ActionExtension('servers', 'gc_discard',
                                                    self._discard_instance))

        actions.append(extensions.ActionExtension('servers', 'gc_list_launched',
                                                    self._list_launched_instances))

        actions.append(extensions.ActionExtension('servers', 'gc_list_blessed',
                                                    self._list_blessed_instances))

        return actions

    @convert_exception
    def _bless_instance(self, input_dict, req, id):
        context = req.environ["nova.context"]
        result = self.gridcentric_api.bless_instance(context, id)
        return self._build_instance_list(req, [result])

    @convert_exception
    def _discard_instance(self, input_dict, req, id):
        context = req.environ["nova.context"]
        result = self.gridcentric_api.discard_instance(context, id)
        return webob.Response(status_int=200, body=json.dumps(result))

    @convert_exception
    def _launch_instance(self, input_dict, req, id):
        context = req.environ["nova.context"]
        try:
            params = input_dict.get('gc_launch', {})
            result = self.gridcentric_api.launch_instance(context, id,
                                                          params=params)
            return self._build_instance_list(req, [result])
        except quota.QuotaError as error:
            self.server_helper._handle_quota_error(error)

    @convert_exception
    def _migrate_instance(self, input_dict, req, id):
        context = req.environ["nova.context"]
        try:
            dest = input_dict["gc_migrate"].get("dest", None)
            self.gridcentric_api.migrate_instance(context, id, dest)
            return webob.Response(status_int=200)
        except quota.QuotaError as error:
            self.server_helper._handle_quota_error(error)

    @convert_exception
    def _list_launched_instances(self, input_dict, req, id):
        context = req.environ["nova.context"]
        return self._build_instance_list(req, self.gridcentric_api.list_launched_instances(context, id))

    @convert_exception
    def _list_blessed_instances(self, input_dict, req, id):
        context = req.environ["nova.context"]
        return self._build_instance_list(req, self.gridcentric_api.list_blessed_instances(context, id))

    def _build_instance_list(self, req, instances):
        def _build_view(req, instance, is_detail=True):
            project_id = getattr(req.environ['nova.context'], 'project_id', '')
            base_url = req.application_url
            flavor_builder = nova.api.openstack.views.flavors.ViewBuilderV11(
                base_url, project_id)
            image_builder = nova.api.openstack.views.images.ViewBuilderV11(
                base_url, project_id)
            addresses_builder = nova.api.openstack.views.addresses.ViewBuilderV11()
            builder = nova.api.openstack.views.servers.ViewBuilderV11(
                addresses_builder, flavor_builder, image_builder,
                base_url, project_id)
            return builder.build(instance, is_detail=is_detail)
        result = [_build_view(req, inst)['server']
                    for inst in instances]

        return webob.Response(status_int=200, body=json.dumps(result))