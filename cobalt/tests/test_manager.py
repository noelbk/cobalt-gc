# Copyright 2011 Gridcentric Inc.
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

import unittest
import os
import shutil

from datetime import datetime

from nova import db
from nova import context as nova_context
from nova import exception

from nova.compute import vm_states
from nova.compute import task_states
from nova.compute import power_state

from oslo.config import cfg

import cobalt.nova.extension.manager as co_manager
import cobalt.tests.utils as utils
import cobalt.nova.extension.vmsconn as vmsconn
from cobalt.tests.mocks.instances import Instance
from cobalt.tests.mocks.volumes import MockVolumeApi, Volume
from cobalt.tests.mocks.networks import MockNetworkApi, Network

CONF = cfg.CONF

class CobaltManagerTestCase(unittest.TestCase):

    def setUp(self):
        CONF.compute_driver = 'fake.FakeDriver'
        CONF.set_override('use_local', True, group='conductor')

        # Mock out all of the policy enforcement (the tests don't have a defined policy)
        utils.mock_policy()

        # Copy the clean database over
        shutil.copyfile(os.path.join(CONF.state_path, CONF.sqlite_clean_db),
                        os.path.join(CONF.state_path, CONF.sqlite_db))

        self.mock_rpc = utils.mock_rpc

        self.vmsconn = utils.MockVmsConn()
        self.cobalt = co_manager.CobaltManager(vmsconn=self.vmsconn,
            volume_api=MockVolumeApi(), network_api=MockNetworkApi())

        self.context = nova_context.RequestContext('fake', 'fake', True)

    def test_target_memory_string_conversion_case_insensitive(self):

        # Ensures case insensitive
        self.assertEquals(co_manager.memory_string_to_pages('512MB'),
                          co_manager.memory_string_to_pages('512mB'))
        self.assertEquals(co_manager.memory_string_to_pages('512mB'),
                          co_manager.memory_string_to_pages('512Mb'))
        self.assertEquals(co_manager.memory_string_to_pages('512mB'),
                          co_manager.memory_string_to_pages('512mb'))

    def test_target_memory_string_conversion_value(self):
        # Check conversion of units.
        self.assertEquals(268435456, co_manager.memory_string_to_pages('1TB'))
        self.assertEquals(137438953472, co_manager.memory_string_to_pages('512TB'))

        self.assertEquals(262144, co_manager.memory_string_to_pages('1GB'))

        self.assertEquals(256, co_manager.memory_string_to_pages('1MB'))
        self.assertEquals(131072, co_manager.memory_string_to_pages('512MB'))

        self.assertEquals(1, co_manager.memory_string_to_pages('2KB'))
        self.assertEquals(1, co_manager.memory_string_to_pages('4KB'))
        self.assertEquals(5, co_manager.memory_string_to_pages('20KB'))

        self.assertEquals(2, co_manager.memory_string_to_pages('12287b'))
        self.assertEquals(3, co_manager.memory_string_to_pages('12288b'))
        self.assertEquals(1, co_manager.memory_string_to_pages('512'))
        self.assertEquals(1, co_manager.memory_string_to_pages('4096'))
        self.assertEquals(2, co_manager.memory_string_to_pages('8192'))

    def test_target_memory_string_conversion_case_unconvertible(self):
        # Check against garbage inputs
        try:
            co_manager.memory_string_to_pages('512megabytes')
            self.fail("Should not be able to convert '512megabytes'")
        except ValueError:
            pass

        try:
            co_manager.memory_string_to_pages('garbage')
            self.fail("Should not be able to convert 'garbage'")
        except ValueError:
            pass

        try:
            co_manager.memory_string_to_pages('-512MB')
            self.fail("Should not be able to convert '-512MB'")
        except ValueError:
            pass

    def test_bless_instance(self):

        self.vmsconn.set_return_val("bless",
                                    ("newname", "migration_url", ["file1", "file2", "file3"],[]))
        self.vmsconn.set_return_val("post_bless", ["file1_ref", "file2_ref", "file3_ref"])
        self.vmsconn.set_return_val("bless_cleanup", None)
        self.vmsconn.set_return_val("get_instance_info",
                                    {'state': power_state.RUNNING})

        pre_bless_time = datetime.utcnow()
        instance = Instance(self.context).create()
        blessed = Instance(self.context).isBlessed(instance=instance).create()
        migration_url, instance_ref = self.cobalt.bless_instance(
                                                    self.context,
                                                    instance_uuid=blessed['uuid'],
                                                    migration_url=None)

        blessed_instance = db.instance_get_by_uuid(self.context, blessed['uuid'])
        self.assertEquals("blessed", blessed_instance['vm_state'])
        self.assertEquals("migration_url", migration_url)
        system_metadata = db.instance_system_metadata_get(self.context, blessed['uuid'])
        self.assertEquals("file1_ref,file2_ref,file3_ref", system_metadata['images'])

        self.assertTrue(pre_bless_time <= blessed_instance['launched_at'])

        self.assertTrue(blessed_instance['disable_terminate'])

    def test_bless_instance_exception(self):
        self.vmsconn.set_return_val("bless", utils.TestInducedException())
        self.vmsconn.set_return_val("get_instance_info",
            {'state': power_state.RUNNING})
        self.vmsconn.set_return_val("unpause_instance", None)

        blessed_uuid = utils.create_pre_blessed_instance(self.context)

        blessed_instance = db.instance_get_by_uuid(self.context, blessed_uuid)
        self.assertTrue(blessed_instance['disable_terminate'])

        try:
            self.cobalt.bless_instance(self.context,
                                       instance_uuid=blessed_uuid,
                                       migration_url=None)
            self.fail("The bless error should have been re-raised up.")
        except utils.TestInducedException:
            pass

        blessed_instance = db.instance_get_by_uuid(self.context, blessed_uuid)
        self.assertEquals(vm_states.ERROR, blessed_instance['vm_state'])
        system_metadata = db.instance_system_metadata_get(self.context, blessed_uuid)
        self.assertEquals(None, system_metadata.get('images', None))
        self.assertEquals(None, system_metadata.get('blessed', None))
        self.assertEquals(None, blessed_instance['launched_at'])
        self.assertTrue(blessed_instance['disable_terminate'])

    def test_bless_instance_not_found(self):

        # Create a new UUID for a non existing instance.
        blessed_uuid = utils.create_uuid()
        try:
            self.cobalt.bless_instance(self.context, instance_uuid=blessed_uuid,
                                            migration_url=None)
            self.fail("Bless should have thrown InstanceNotFound exception.")
        except exception.InstanceNotFound:
            pass

    def test_bless_instance_migrate(self):
        self.vmsconn.set_return_val("bless",
                                    ("newname", "migration_url", ["file1", "file2", "file3"], []))
        self.vmsconn.set_return_val("post_bless", ["file1_ref", "file2_ref", "file3_ref"])
        self.vmsconn.set_return_val("bless_cleanup", None)
        self.vmsconn.set_return_val("get_instance_info",
            {'state': power_state.RUNNING})

        blessed_uuid = utils.create_instance(self.context)
        pre_bless_instance = db.instance_get_by_uuid(self.context, blessed_uuid)
        migration_url, instance_ref = self.cobalt.bless_instance(
                                          self.context,
                                          instance_uuid=blessed_uuid,
                                          migration_url="mcdist://migrate_addr")
        post_bless_instance = db.instance_get_by_uuid(self.context, blessed_uuid)

        self.assertEquals(pre_bless_instance['vm_state'], post_bless_instance['vm_state'])
        self.assertEquals("migration_url", migration_url)
        system_metadata = db.instance_system_metadata_get(self.context, blessed_uuid)
        self.assertEquals("file1_ref,file2_ref,file3_ref", system_metadata['images'])
        self.assertEquals(pre_bless_instance['launched_at'], post_bless_instance['launched_at'])
        self.assertFalse(pre_bless_instance.get('disable_terminate', None),
                         post_bless_instance.get('disable_terminate', None))

    def test_launch_instance(self):

        self.vmsconn.set_return_val("launch", None)
        blessed_uuid = utils.create_blessed_instance(self.context)
        launched_uuid = utils.create_pre_launched_instance(self.context,
                                                source_uuid=blessed_uuid)

        pre_launch_time = datetime.utcnow()
        self.cobalt.launch_instance(self.context, instance_uuid=launched_uuid)

        launched_instance = db.instance_get_by_uuid(self.context, launched_uuid)
        self.assertNotEquals(None, launched_instance['power_state'])
        self.assertEquals("active", launched_instance['vm_state'])
        self.assertTrue(pre_launch_time <= launched_instance['launched_at'])
        self.assertEquals(None, launched_instance['task_state'])
        self.assertEquals(self.cobalt.host, launched_instance['host'])
        self.assertEquals(self.cobalt.nodename, launched_instance['node'])

        # Ensure the proper vms policy is passed into vmsconn
        self.assertEquals(';blessed=%s;;flavor=m1.tiny;;tenant=fake;;uuid=%s;'\
                             % (blessed_uuid, launched_uuid),
            self.vmsconn.params_passed[0]['kwargs']['vms_policy'])

    def test_launch_instance_images(self):
        self.vmsconn.set_return_val("launch", None)
        blessed_uuid = utils.create_blessed_instance(self.context,
            instance={'system_metadata':{'images':'image1'}})

        instance = db.instance_get_by_uuid(self.context, blessed_uuid)
        system_metadata = db.instance_system_metadata_get(self.context, instance['uuid'])
        self.assertEquals('image1', system_metadata.get('images', ''))

        launched_uuid = utils.create_pre_launched_instance(self.context, source_uuid=blessed_uuid)

        self.cobalt.launch_instance(self.context, instance_uuid=launched_uuid)

        # Ensure that image1 was passed to vmsconn.launch
        self.assertEquals(['image1'], self.vmsconn.params_passed[0]['kwargs']['image_refs'])

    def test_launch_instance_blessed_networks(self):

        self.vmsconn.set_return_val("launch", None)

        network = Network(self.context, self.cobalt.network_api,
                         name='blessed_network').create()

        # This is an unsed network associated with the project
        Network(self.context, self.cobalt.network_api).create()

        instance = Instance(self.context).plugged(network).create()
        blessed = Instance(self.context).isBlessed(instance=instance).create()
        pre_launch = Instance(self.context).isPrelaunched(
                instance=blessed).create()

        self.cobalt.launch_instance(self.context,
            instance_uuid=pre_launch['uuid'])

        network_info = self.vmsconn.params_passed[0]['args'][3]
        self.assertTrue(len(network_info) == 1)
        self.assertEquals('blessed_network', network_info[0]['network']['label'])

    def test_launch_instance_requested_networks(self):

        self.vmsconn.set_return_val("launch", None)

        bless_network = Network(self.context, self.cobalt.network_api,
            name='blessed_network').create()

        # This is an unsed network associated with the project
        launch_network = Network(self.context, self.cobalt.network_api).create()

        instance = Instance(self.context).plugged(bless_network).create()
        blessed = Instance(self.context).isBlessed(instance=instance).create()
        pre_launch = Instance(self.context).isPrelaunched(
            instance=blessed).create()

        requested_networks = [(launch_network._name, None)]
        self.cobalt.launch_instance(self.context,
                                    instance_uuid=pre_launch['uuid'],
                                    params={'networks': requested_networks})

        network_info = self.vmsconn.params_passed[0]['args'][3]
        self.assertTrue(len(network_info) == 1)
        self.assertEquals(launch_network._name,
                          network_info[0]['network']['label'])

    def test_launch_instance_exception(self):

        self.vmsconn.set_return_val("launch", utils.TestInducedException())
        launched_uuid = utils.create_pre_launched_instance(self.context)

        try:
            self.cobalt.launch_instance(self.context, instance_uuid=launched_uuid)
            self.fail("The exception from launch should be re-raised up.")
        except utils.TestInducedException:
            pass

        launched_instance = db.instance_get_by_uuid(self.context, launched_uuid)
        self.assertEquals("error", launched_instance['vm_state'])
        self.assertEquals(None, launched_instance['task_state'])
        self.assertEquals(None, launched_instance['launched_at'])
        self.assertEquals(self.cobalt.host, launched_instance['host'])
        self.assertEquals(self.cobalt.nodename, launched_instance['node'])

    def test_launch_instance_migrate(self):

        self.vmsconn.set_return_val("launch", None)
        instance_uuid = utils.create_instance(self.context, {'vm_state': vm_states.ACTIVE})
        pre_launch_instance = db.instance_get_by_uuid(self.context, instance_uuid)

        self.cobalt.launch_instance(self.context, instance_uuid=instance_uuid,
                                         migration_url="migration_url")

        post_launch_instance = db.instance_get_by_uuid(self.context, instance_uuid)

        self.assertEquals(vm_states.ACTIVE, post_launch_instance['vm_state'])
        self.assertEquals(None, post_launch_instance['task_state'])
        self.assertEquals(pre_launch_instance['launched_at'], post_launch_instance['launched_at'])
        self.assertEquals(self.cobalt.host, post_launch_instance['host'])
        self.assertEquals(self.cobalt.nodename, post_launch_instance['node'])

    def test_launch_instance_migrate_exception(self):

        self.vmsconn.set_return_val("launch", utils.TestInducedException())
        launched_uuid = utils.create_instance(self.context, {'vm_state': vm_states.ACTIVE})

        try:
            self.cobalt.launch_instance(self.context, instance_uuid=launched_uuid,
                                             migration_url="migration_url")
            self.fail("The launch error should have been re-raised up.")
        except utils.TestInducedException:
            pass

        launched_instance = db.instance_get_by_uuid(self.context, launched_uuid)
        # (dscannell): This needs to be fixed up once we have the migration state transitions
        # performed correctly.
        self.assertEquals(vm_states.ACTIVE, launched_instance['vm_state'])
        self.assertEquals(task_states.SPAWNING, launched_instance['task_state'])
        self.assertEquals(None, launched_instance['launched_at'])
        self.assertEquals(None, launched_instance['host'])
        self.assertEquals(None, launched_instance['node'])


    def test_discard_a_blessed_instance(self):
        self.vmsconn.set_return_val("discard", None)
        blessed_uuid = utils.create_blessed_instance(self.context, source_uuid="UNITTEST_DISCARD")

        pre_discard_time = datetime.utcnow()
        self.cobalt.discard_instance(self.context, instance_uuid=blessed_uuid)

        try:
            db.instance_get(self.context, blessed_uuid)
            self.fail("The blessed instance should no longer exists after being discarded.")
        except exception.InstanceNotFound:
            # This ensures that the instance has been marked as deleted in the database. Now assert
            # that the rest of its attributes have been marked.
            self.context.read_deleted = 'yes'
            instances = db.instance_get_all(self.context)

            self.assertEquals(1, len(instances))
            discarded_instance = instances[0]

            self.assertTrue(pre_discard_time <= discarded_instance['terminated_at'])
            self.assertEquals(vm_states.DELETED, discarded_instance['vm_state'])

    def test_reset_host_different_host_instance(self):

        host = "test-host"
        instance_uuid = utils.create_instance(self.context,
                                             {'task_state':task_states.MIGRATING,
                                              'host': host})
        self.cobalt.host = 'different-host'
        self.cobalt._refresh_host(self.context)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEquals(host, instance['host'])
        self.assertEquals(task_states.MIGRATING, instance['task_state'])


    def test_reset_host_locked_instance(self):

        host = "test-host"
        locked_instance_uuid = utils.create_instance(self.context,
                                                     {'task_state':task_states.MIGRATING,
                                                      'host': host})
        self.cobalt.host = host
        self.cobalt._lock_instance(locked_instance_uuid)
        self.cobalt._refresh_host(self.context)

        instance = db.instance_get_by_uuid(self.context, locked_instance_uuid)
        self.assertEquals(host, instance['host'])
        self.assertEquals(task_states.MIGRATING, instance['task_state'])

    def test_reset_host_non_migrating_instance(self):

        host = "test-host"
        instance_uuid = utils.create_instance(self.context,
                                             {'host': host})
        self.cobalt.host = host
        self.cobalt._refresh_host(self.context)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEquals(host, instance['host'])
        self.assertEquals(None, instance['task_state'])

    def test_reset_host_local_src(self):

        src_host = "src-test-host"
        dst_host = "dst-test-host"
        instance_uuid = utils.create_instance(self.context,
                                             {'task_state':task_states.MIGRATING,
                                              'host': src_host,
                                              'system_metadata': {'gc_src_host': src_host,
                                                                  'gc_dst_host': dst_host}},
                                            driver=self.cobalt.compute_manager.driver)
        self.cobalt.host = src_host
        self.cobalt._refresh_host(self.context)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEquals(src_host, instance['host'])
        self.assertEquals(None, instance['task_state'])

    def test_reset_host_local_dst(self):

        src_host = "src-test-host"
        dst_host = "dst-test-host"
        instance_uuid = utils.create_instance(self.context,
                                             {'task_state':task_states.MIGRATING,
                                              'host': dst_host,
                                              'system_metadata': {'gc_src_host': src_host,
                                                                  'gc_dst_host': dst_host}},
                                            driver=self.cobalt.compute_manager.driver)
        self.cobalt.host = dst_host
        self.cobalt._refresh_host(self.context)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEquals(dst_host, instance['host'])
        self.assertEquals(None, instance['task_state'])

    def test_reset_host_not_local_src(self):

        src_host = "src-test-host"
        dst_host = "dst-test-host"
        instance_uuid = utils.create_instance(self.context,
                                             {'task_state':task_states.MIGRATING,
                                              'host': src_host,
                                              'system_metadata': {'gc_src_host': src_host,
                                                                  'gc_dst_host': dst_host}})
        self.cobalt.host = src_host
        self.cobalt._refresh_host(self.context)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEquals(dst_host, instance['host'])
        self.assertEquals(task_states.MIGRATING, instance['task_state'])

    def test_reset_host_not_local_dst(self):

        src_host = "src-test-host"
        dst_host = "dst-test-host"
        instance_uuid = utils.create_instance(self.context,
                                             {'task_state':task_states.MIGRATING,
                                              'host': dst_host,
                                              'system_metadata': {'gc_src_host': src_host,
                                                                  'gc_dst_host': dst_host}})
        self.cobalt.host = dst_host
        self.cobalt._refresh_host(self.context)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEquals(dst_host, instance['host'])
        self.assertEquals(None, instance['task_state'])
        self.assertEquals(vm_states.ERROR, instance['vm_state'])

    def test_vms_policy_generation_custom_flavor(self):
        flavor = utils.create_flavor()
        instance_uuid = utils.create_instance(self.context, {'instance_type_id': flavor['id']})
        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        vms_policy = self.cobalt._generate_vms_policy_name(self.context, instance, instance)
        expected_policy = ';blessed=%s;;flavor=%s;;tenant=%s;;uuid=%s;' \
                          %(instance['uuid'], flavor['name'], self.context.project_id, instance['uuid'])
        self.assertEquals(expected_policy, vms_policy)

    def test_vms_policy_deleted_flavor(self):

        flavor = utils.create_flavor()
        instance_uuid = utils.create_instance(self.context,
                {'instance_type_id': flavor['id']})
        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        db.flavor_destroy(self.context, flavor['name'])

        vms_policy = self.cobalt._generate_vms_policy_name(self.context,
                instance, instance)
        expected_policy = ';blessed=%s;;flavor=%s;;tenant=%s;;uuid=%s;' % \
                          (instance['uuid'], flavor['name'],
                           self.context.project_id, instance['uuid'])

        self.assertEquals(expected_policy, vms_policy)

    def test_snapshot_volumes(self):

        volume1 = Volume(self.context, self.cobalt.volume_api).create()
        volume2 = Volume(self.context, self.cobalt.volume_api).create()
        instance = Instance(self.context).attach(volume1)\
                        .attach(volume2).create()

        self.cobalt._snapshot_attached_volumes(self.context, instance, instance,
                is_paused=True)

        for volume in [volume1, volume2]:
            self.assertEquals(1, len(volume._snapshots))
            self.assertEquals("snapshot for %s" % (instance['display_name']),
                        volume._snapshots.values()[0]['display_name'])
            self.assertEquals("snapshot of volume %s" %
                              (volume._volume['id']),
                volume._snapshots.values()[0]['display_description'])

    def test_bless_instance_with_volume(self):

        self.vmsconn.set_return_val("bless",
            ("newname", "migration_url", ["file1", "file2", "file3"], []))
        self.vmsconn.set_return_val("post_bless", ["file1_ref", "file2_ref", "file3_ref"])
        self.vmsconn.set_return_val("bless_cleanup", None)
        self.vmsconn.set_return_val("get_instance_info",
        {'state': power_state.RUNNING})

        volume = Volume(self.context, self.cobalt.volume_api).create()
        instance = Instance(self.context).attach(volume).create()

        self.cobalt.bless_instance(self.context, instance_uuid=instance['uuid'],
                migration_url="mcdist://migrate_addr")

        # Ensure that the volume is detached from the instance.
        self.assertFalse('instance_uuid' in volume._volume)

    def test_launch_instance_with_volume(self):

        self.vmsconn.set_return_val("launch", None)

        volume = Volume(self.context, self.cobalt.volume_api).create()

        instance = Instance(self.context).attach(volume).create()
        blessed = Instance(self.context).isBlessed(instance=instance).create()
        pre_launch = Instance(self.context).isPrelaunched(
                    instance=blessed).create()

        self.cobalt.launch_instance(self.context,
            instance_uuid=pre_launch['uuid'])

        # Ensure a new volume was created and attached to the instance.
        bdm = db.block_device_mapping_get_all_by_instance(self.context,
            pre_launch['uuid'])

        # Ensure that a volume was created from the snapshot
        self.assertEquals(2, len(self.cobalt.volume_api.list_volumes()))
        self.assertEquals(1, len(bdm))
        self.assertTrue(bdm[0]['snapshot_id'] is not None)
        self.assertTrue(bdm[0]['volume_id'] is not None)
        self.assertNotEqual(volume._volume['id'], bdm[0]['volume_id'])


