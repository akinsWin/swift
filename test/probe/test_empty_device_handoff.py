#!/usr/bin/python -u
# Copyright (c) 2010-2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import shutil
import time

from unittest import main
from uuid import uuid4

from swiftclient import client

from swift.common import direct_client
from swift.obj.diskfile import get_data_dir
from swift.common.exceptions import ClientException
from test.probe.common import kill_server, ReplProbeTest, start_server
from swift.common.utils import readconf
from swift.common.manager import Manager


class TestEmptyDevice(ReplProbeTest):

    def _get_objects_dir(self, onode):
        device = onode['device']
        node_id = (onode['port'] - 6000) / 10
        obj_server_conf = readconf(self.configs['object-server'][node_id])
        devices = obj_server_conf['app:object-server']['devices']
        obj_dir = '%s/%s' % (devices, device)
        return obj_dir

    def test_main(self):
        # Create container
        # Kill one container/obj primary server
        # Delete the default data directory for objects on the primary server
        # Create container/obj (goes to two primary servers and one handoff)
        # Kill other two container/obj primary servers
        # Indirectly through proxy assert we can get container/obj
        # Restart those other two container/obj primary servers
        # Directly to handoff server assert we can get container/obj
        # Assert container listing (via proxy and directly) has container/obj
        # Bring the first container/obj primary server back up
        # Assert that it doesn't have container/obj yet
        # Run object replication for first container/obj primary server
        # Run object replication for handoff node
        # Assert the first container/obj primary server now has container/obj
        # Assert the handoff server no longer has container/obj

        container = 'container-%s' % uuid4()
        client.put_container(self.url, self.token, container)

        cpart, cnodes = self.container_ring.get_nodes(self.account, container)
        cnode = cnodes[0]
        obj = 'object-%s' % uuid4()
        opart, onodes = self.object_ring.get_nodes(
            self.account, container, obj)
        onode = onodes[0]
        kill_server(onode['port'], self.port2server, self.pids)
        obj_dir = '%s/%s' % (self._get_objects_dir(onode),
                             get_data_dir(self.policy.idx))
        shutil.rmtree(obj_dir, True)
        self.assertFalse(os.path.exists(obj_dir))
        client.put_object(self.url, self.token, container, obj, 'VERIFY')
        odata = client.get_object(self.url, self.token, container, obj)[-1]
        if odata != 'VERIFY':
            raise Exception('Object GET did not return VERIFY, instead it '
                            'returned: %s' % repr(odata))
        # Kill all primaries to ensure GET handoff works
        for node in onodes[1:]:
            kill_server(node['port'], self.port2server, self.pids)
        odata = client.get_object(self.url, self.token, container, obj)[-1]
        if odata != 'VERIFY':
            raise Exception('Object GET did not return VERIFY, instead it '
                            'returned: %s' % repr(odata))
        for node in onodes[1:]:
            start_server(node['port'], self.port2server, self.pids)
            self.assertFalse(os.path.exists(obj_dir))
            # We've indirectly verified the handoff node has the object, but
            # let's directly verify it.
        another_onode = self.object_ring.get_more_nodes(opart).next()
        odata = direct_client.direct_get_object(
            another_onode, opart, self.account, container, obj,
            headers={'X-Backend-Storage-Policy-Index': self.policy.idx})[-1]
        if odata != 'VERIFY':
            raise Exception('Direct object GET did not return VERIFY, instead '
                            'it returned: %s' % repr(odata))
        objs = [o['name'] for o in
                client.get_container(self.url, self.token, container)[1]]
        if obj not in objs:
            raise Exception('Container listing did not know about object')
        timeout = time.time() + 5
        found_objs_on_cnode = []
        while time.time() < timeout:
            for cnode in [c for c in cnodes if cnodes not in
                          found_objs_on_cnode]:
                objs = [o['name'] for o in
                        direct_client.direct_get_container(
                            cnode, cpart, self.account, container)[1]]
                if obj in objs:
                    found_objs_on_cnode.append(cnode)
            if len(found_objs_on_cnode) >= len(cnodes):
                break
            time.sleep(0.3)
        if len(found_objs_on_cnode) < len(cnodes):
            missing = ['%s:%s' % (cnode['ip'], cnode['port']) for cnode in
                       cnodes if cnode not in found_objs_on_cnode]
            raise Exception('Container servers %r did not know about object' %
                            missing)
        start_server(onode['port'], self.port2server, self.pids)
        self.assertFalse(os.path.exists(obj_dir))
        exc = None
        try:
            direct_client.direct_get_object(
                onode, opart, self.account, container, obj, headers={
                    'X-Backend-Storage-Policy-Index': self.policy.idx})
        except ClientException as err:
            exc = err
        self.assertEquals(exc.http_status, 404)
        self.assertFalse(os.path.exists(obj_dir))

        try:
            port_num = onode['replication_port']
        except KeyError:
            port_num = onode['port']
        try:
            another_port_num = another_onode['replication_port']
        except KeyError:
            another_port_num = another_onode['port']

        num = (port_num - 6000) / 10
        Manager(['object-replicator']).once(number=num)

        another_num = (another_port_num - 6000) / 10
        Manager(['object-replicator']).once(number=another_num)

        odata = direct_client.direct_get_object(
            onode, opart, self.account, container, obj, headers={
                'X-Backend-Storage-Policy-Index': self.policy.idx})[-1]
        if odata != 'VERIFY':
            raise Exception('Direct object GET did not return VERIFY, instead '
                            'it returned: %s' % repr(odata))
        exc = None
        try:
            direct_client.direct_get_object(
                another_onode, opart, self.account, container, obj, headers={
                    'X-Backend-Storage-Policy-Index': self.policy.idx})
        except ClientException as err:
            exc = err
        self.assertEquals(exc.http_status, 404)

if __name__ == '__main__':
    main()
