#!/usr/bin/env python3
"""Glance Operator Charm.

This charm provide Glance services as part of an OpenStack deployment
"""

import logging
from typing import Callable
from typing import List

from ops.framework import StoredState
from ops.main import main
from ops.model import BlockedStatus
from ops.charm import CharmBase

import advanced_sunbeam_openstack.charm as sunbeam_charm
import advanced_sunbeam_openstack.core as sunbeam_core
import advanced_sunbeam_openstack.relation_handlers as sunbeam_rhandlers
import advanced_sunbeam_openstack.config_contexts as sunbeam_ctxts

logger = logging.getLogger(__name__)


class GlanceStorageRelationHandler(sunbeam_rhandlers.CephClientHandler):
    """A relation handler for optional glance storage relations.

    This will claim ready if there is local storage that is available in
    order to configure the glance local registry. If there is a ceph
    relation, then this will wait until the ceph relation is fulfilled before
    claiming it is ready.
    """

    def __init__(
        self,
        charm: CharmBase,
        relation_name: str,
        callback_f: Callable,
        allow_ec_overwrites: bool = True,
        app_name: str = None,
        juju_storage_name: str = None,
    ) -> None:
        """Run constructor."""
        self.juju_storage_name = juju_storage_name
        super().__init__(charm, relation_name, callback_f, allow_ec_overwrites,
                         app_name)

    @property
    def ready(self) -> bool:
        """Determines if the ceph relation is ready or not.

        This relation will be ready in one of the following conditions:
         * If the ceph-client relation exists, then the ceph-client relation
           must be ready as per the parent class
         * If the ceph-client relation does not exist, and local storage has
           been provided, then this will claim it is ready

        If none of the above are valid, then this will return False causing
        the charm to go into a waiting state.

        :return: True if the storage is ready, False otherwise.
        """
        if self.charm.has_ceph_relation():
            logger.debug(f'ceph relation is connected, deferring to parent')
            return super().ready

        # Check to see if the storage is satisfied
        if self.charm.has_local_storage():
            logger.debug(f'Storage {self.juju_storage_name} is attached')
            return True

        logger.debug('Ceph relation does not exist and no local storage is '
                     'available.')
        return False

    def context(self) -> dict:
        """

        :return:
        """
        if self.charm.has_ceph_relation():
            return super().context()
        return {}


class GlanceOperatorCharm(sunbeam_charm.OSBaseOperatorAPICharm):
    """Charm the service."""

    ceph_conf = "/etc/ceph/ceph.conf"

    _state = StoredState()
    _authed = False
    service_name = "glance-api"
    wsgi_admin_script = '/usr/bin/glance-wsgi-api'
    wsgi_public_script = '/usr/bin/glance-wsgi-api'

    db_sync_cmds = [
        ['sudo', '-u', 'glance', 'glance-manage', '--config-dir',
         '/etc/glance', 'db', 'sync']]

    @property
    def config_contexts(self) -> List[sunbeam_ctxts.ConfigContext]:
        """Configuration contexts for the operator."""
        contexts = super().config_contexts
        if self.has_ceph_relation():
            logger.debug('Application has ceph relation')
            contexts.append(
                sunbeam_ctxts.CephConfigurationContext(self, "ceph_config"))
            contexts.append(
                sunbeam_ctxts.CinderCephConfigurationContext(self,
                                                             "cinder_ceph"))
        return contexts

    @property
    def container_configs(self) -> List[sunbeam_core.ContainerConfigFile]:
        """Container configurations for the operator."""
        _cconfigs = super().container_configs
        if self.has_ceph_relation():
            _cconfigs.extend(
                [
                    sunbeam_core.ContainerConfigFile(
                        self.ceph_conf,
                        self.service_user,
                        self.service_group,
                    ),
                ]
            )
        return _cconfigs

    def get_relation_handlers(self) -> List[sunbeam_rhandlers.RelationHandler]:
        """Relation handlers for the service."""
        handlers = super().get_relation_handlers()
        self.ceph = GlanceStorageRelationHandler(
            self,
            "ceph",
            self.configure_charm,
            allow_ec_overwrites=True,
            app_name='rbd',
            juju_storage_name='local-repository',
        )
        handlers.append(self.ceph)
        return handlers

    @property
    def service_conf(self) -> str:
        """Service default configuration file."""
        return f"/etc/glance/glance-api.conf"

    @property
    def service_user(self) -> str:
        """Service user file and directory ownership."""
        return 'glance'

    @property
    def service_group(self) -> str:
        """Service group file and directory ownership."""
        return 'glance'

    @property
    def service_endpoints(self):
        return [
            {
                'service_name': 'glance',
                'type': 'image',
                'description': "OpenStack Image",
                'internal_url': f'{self.internal_url}',
                'public_url': f'{self.public_url}',
                'admin_url': f'{self.admin_url}'}]

    @property
    def default_public_ingress_port(self):
        return 9292

    def has_local_storage(self) -> bool:
        """Returns whether the application has been deployed with local
        storage or not.

        :return: True if local storage is present, False otherwise
        """
        storages = self.model.storages['local-repository']
        return len(storages) > 0

    def has_ceph_relation(self) -> bool:
        """Returns whether or not the application has been related to Ceph.

        :return: True if the ceph relation has been made, False otherwise.
        """
        return self.model.get_relation('ceph') is not None

    def configure_charm(self, event) -> None:
        """Catchall handler to cconfigure charm services."""
        if not self.relation_handlers_ready():
            logging.debug("Deferring configuration, charm relations not ready")
            return

        if self.has_ceph_relation():
            if not self.ceph.key:
                logger.debug('Ceph key is not yet present, waiting.')
                return
        elif self.has_local_storage():
            logger.debug('Local storage is configured, using that.')
        else:
            logger.debug('Neither local storage nor ceph relation exists.')
            self.unit.status = BlockedStatus('Missing storage. Relate to Ceph '
                                             'or add local storage to '
                                             'continue.')
            return

        ph = self.get_named_pebble_handler("glance-api")
        if ph.pebble_ready:
            if self.has_ceph_relation() and self.ceph.key:
                logger.debug('Setting up Ceph packages in images.')
                ph.execute(
                    ['apt', 'update'],
                    exception_on_error=True)
                ph.execute(
                    ['apt', 'install', '-y', 'ceph-common'],
                    exception_on_error=True)
                ph.execute(
                    [
                        'ceph-authtool',
                        f'/etc/ceph/ceph.client.{self.app.name}.keyring',
                        '--create-keyring',
                        f'--name=client.{self.app.name}',
                        f'--add-key={self.ceph.key}'],
                    exception_on_error=True)
            else:
                logger.debug('Using local storage')
            ph.init_service(self.contexts())

        super().configure_charm(event)
        if self._state.bootstrapped:
            for handler in self.pebble_handlers:
                handler.start_service()


class GlanceXenaOperatorCharm(GlanceOperatorCharm):

    openstack_release = 'xena'


if __name__ == "__main__":
    # Note: use_juju_for_storage=True required per
    # https://github.com/canonical/operator/issues/506
    main(GlanceXenaOperatorCharm, use_juju_for_storage=True)
