# Copyright 2022-2023 XProbe Inc.
# derived from copyright 1999-2021 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import logging
from typing import Dict, List, Optional, Set, Type, TypeVar

from .... import oscar as mo
from ....lib.aio import alru_cache
from ....resource import Resource
from ....typing import BandType
from ...core import NodeRole
from ..core import (
    DiskInfo,
    NodeStatus,
    QuotaInfo,
    StorageInfo,
    WorkerSlotInfo,
    watch_method,
)
from .core import AbstractClusterAPI

APIType = TypeVar("APIType", bound="ClusterAPI")
logger = logging.getLogger(__name__)


class ClusterAPI(AbstractClusterAPI):
    def __init__(self, address: str):
        self._address = address
        self._locator_ref = None
        self._uploader_ref = None

    async def _init(self):
        from ..locator import SupervisorLocatorActor
        from ..uploader import NodeInfoUploaderActor

        self._locator_ref = await mo.actor_ref(
            SupervisorLocatorActor.default_uid(), address=self._address
        )
        self._uploader_ref = await mo.actor_ref(
            NodeInfoUploaderActor.default_uid(), address=self._address
        )

    @classmethod
    @alru_cache(cache_exceptions=False)
    async def create(cls: Type[APIType], address: str) -> APIType:
        api_obj = cls(address)
        await api_obj._init()
        return api_obj

    @alru_cache(cache_exceptions=False)
    async def _get_node_info_ref(self):
        from ..supervisor.node_info import NodeInfoCollectorActor

        [node_info_ref] = await self.get_supervisor_refs(
            [NodeInfoCollectorActor.default_uid()]
        )
        return node_info_ref

    async def get_supervisors(self, filter_ready: bool = True) -> List[str]:
        return await self._locator_ref.get_supervisors(filter_ready=filter_ready)

    @watch_method
    async def watch_supervisors(self, version: Optional[int] = None):
        return await self._locator_ref.watch_supervisors(version=version)

    async def get_supervisors_by_keys(self, keys: List[str]) -> List[str]:
        """
        Get supervisor address hosting the specified key

        Parameters
        ----------
        keys
            key for a supervisor address

        Returns
        -------
        out
            addresses of the supervisor
        """
        get_supervisor = self._locator_ref.get_supervisor
        return await get_supervisor.batch(*(get_supervisor.delay(k) for k in keys))

    @watch_method
    async def watch_supervisors_by_keys(
        self, keys: List[str], version: Optional[int] = None
    ):
        return await self._locator_ref.watch_supervisors_by_keys(keys, version=version)

    async def get_supervisor_refs(self, uids: List[str]) -> List[mo.ActorRef]:
        """
        Get actor references hosting the specified actor uid

        Parameters
        ----------
        uids
            uids for a supervisor address
        watch
            if True, will watch changes of supervisor changes

        Returns
        -------
        out : List[mo.ActorRef]
            references of the actors
        """
        addrs = await self.get_supervisors_by_keys(uids)
        if any(addr is None for addr in addrs):
            none_uid = next(uid for addr, uid in zip(addrs, uids) if addr is None)
            raise mo.ActorNotExist(f"Actor {none_uid} not exist as no supervisors")

        return await asyncio.gather(
            *[mo.actor_ref(uid, address=addr) for addr, uid in zip(addrs, uids)]
        )

    async def watch_supervisor_refs(self, uids: List[str]):
        async for addrs in self.watch_supervisors_by_keys(uids):
            yield await asyncio.gather(
                *[mo.actor_ref(uid, address=addr) for addr, uid in zip(addrs, uids)]
            )

    @watch_method
    async def watch_nodes(
        self,
        role: NodeRole,
        env: bool = False,
        resource: bool = False,
        detail: bool = False,
        version: Optional[int] = None,
        statuses: Set[NodeStatus] = None,
        exclude_statuses: Set[NodeStatus] = None,
    ) -> List[Dict[str, Dict]]:
        statuses = self._calc_statuses(statuses, exclude_statuses)
        node_info_ref = await self._get_node_info_ref()
        return await node_info_ref.watch_nodes(
            role,
            env=env,
            resource=resource,
            detail=detail,
            statuses=statuses,
            version=version,
        )

    async def get_nodes_info(
        self,
        nodes: List[str] = None,
        role: NodeRole = None,
        env: bool = False,
        resource: bool = False,
        detail: bool = False,
        statuses: Set[NodeStatus] = None,
        exclude_statuses: Set[NodeStatus] = None,
    ) -> Dict[str, Dict]:
        statuses = self._calc_statuses(statuses, exclude_statuses)
        node_info_ref = await self._get_node_info_ref()
        return await node_info_ref.get_nodes_info(
            nodes=nodes,
            role=role,
            env=env,
            resource=resource,
            detail=detail,
            statuses=statuses,
        )

    async def set_node_status(self, node: str, role: NodeRole, status: NodeStatus):
        """
        Set status of node

        Parameters
        ----------
        node : str
            address of node
        role: NodeRole
            role of node
        status : NodeStatus
            status of node
        """
        node_info_ref = await self._get_node_info_ref()
        await node_info_ref.update_node_info(node, role, status=status)

    async def get_all_bands(
        self,
        role: NodeRole = None,
        statuses: Set[NodeStatus] = None,
        exclude_statuses: Set[NodeStatus] = None,
    ) -> Dict[BandType, Resource]:
        statuses = self._calc_statuses(statuses, exclude_statuses)
        node_info_ref = await self._get_node_info_ref()
        return await node_info_ref.get_all_bands(role, statuses=statuses)

    @watch_method
    async def watch_all_bands(
        self,
        role: NodeRole = None,
        version: Optional[int] = None,
        statuses: Set[NodeStatus] = None,
        exclude_statuses: Set[NodeStatus] = None,
    ):
        statuses = self._calc_statuses(statuses, exclude_statuses)
        node_info_ref = await self._get_node_info_ref()
        return await node_info_ref.watch_all_bands(
            role, statuses=statuses, version=version
        )

    async def get_mars_versions(self) -> List[str]:
        node_info_ref = await self._get_node_info_ref()
        return await node_info_ref.get_mars_versions()

    async def get_bands(self) -> Dict:
        """
        Get bands that can be used for computation on current node.

        Returns
        -------
        band_to_resource : dict
            Band to resource.
        """
        return await self._uploader_ref.get_bands()

    async def mark_node_ready(self):
        """
        Mark current node ready for work loads
        """
        await self._uploader_ref.mark_node_ready()

    async def wait_node_ready(self):
        """
        Wait current node to be ready
        """
        await self._uploader_ref.wait_node_ready()

    async def wait_all_supervisors_ready(self):
        """
        Wait till all expected supervisors are ready
        """
        await self._locator_ref.wait_all_supervisors_ready()

    async def set_band_slot_infos(
        self, band_name: str, slot_infos: List[WorkerSlotInfo]
    ):
        await self._uploader_ref.set_band_slot_infos.tell(band_name, slot_infos)

    async def set_band_quota_info(self, band_name: str, quota_info: QuotaInfo):
        await self._uploader_ref.set_band_quota_info.tell(band_name, quota_info)

    async def set_node_disk_info(self, disk_info: List[DiskInfo]):
        await self._uploader_ref.set_node_disk_info(disk_info)

    @mo.extensible
    async def set_band_storage_info(self, band_name: str, storage_info: StorageInfo):
        await self._uploader_ref.set_band_storage_info(band_name, storage_info)

    async def request_worker(
        self, worker_cpu: int = None, worker_mem: int = None, timeout: int = None
    ) -> str:
        node_allocator_ref = await self._get_node_allocator_ref()
        address = await node_allocator_ref.request_worker(
            worker_cpu, worker_mem, timeout
        )
        return address

    async def release_worker(self, address: str):
        node_allocator_ref = await self._get_node_allocator_ref()
        await node_allocator_ref.release_worker(address)
        node_info_ref = await self._get_node_info_ref()
        await node_info_ref.update_node_info(
            address, NodeRole.WORKER, status=NodeStatus.STOPPED
        )

    async def reconstruct_worker(self, address: str):
        node_allocator_ref = await self._get_node_allocator_ref()
        await node_allocator_ref.reconstruct_worker(address)

    @alru_cache(cache_exceptions=False)
    async def _get_node_allocator_ref(self):
        from ..supervisor.node_allocator import NodeAllocatorActor

        [node_allocator_ref] = await self.get_supervisor_refs(
            [NodeAllocatorActor.default_uid()]
        )
        return node_allocator_ref

    async def _get_process_info_manager_ref(self, address: str = None):
        from ..procinfo import ProcessInfoManagerActor

        return await mo.actor_ref(
            ProcessInfoManagerActor.default_uid(), address=address or self._address
        )

    async def get_node_pool_configs(self, address: str = None) -> List[Dict]:
        ref = await self._get_process_info_manager_ref(address)
        return await ref.get_pool_configs()

    async def get_node_thread_stacks(
        self, address: str = None
    ) -> List[Dict[int, List[str]]]:
        ref = await self._get_process_info_manager_ref(address)
        return await ref.get_thread_stacks()

    async def _get_log_ref(self, address: str = None):
        from ..file_logger import FileLoggerActor

        return await mo.actor_ref(
            FileLoggerActor.default_uid(), address=address or self._address
        )

    async def fetch_node_log(
        self, size: int, address: str = None, offset: int = 0
    ) -> str:
        ref = await self._get_log_ref(address)
        return await ref.fetch_logs(size, offset)


class MockClusterAPI(ClusterAPI):
    @classmethod
    async def create(cls: Type[APIType], address: str, **kw) -> APIType:
        from ..file_logger import FileLoggerActor
        from ..procinfo import ProcessInfoManagerActor
        from ..supervisor.locator import SupervisorPeerLocatorActor
        from ..supervisor.node_allocator import NodeAllocatorActor
        from ..supervisor.node_info import NodeInfoCollectorActor
        from ..uploader import NodeInfoUploaderActor

        create_actor_coros = [
            mo.create_actor(
                SupervisorPeerLocatorActor,
                "fixed",
                address,
                uid=SupervisorPeerLocatorActor.default_uid(),
                address=address,
            ),
            mo.create_actor(
                NodeInfoCollectorActor,
                uid=NodeInfoCollectorActor.default_uid(),
                address=address,
            ),
            mo.create_actor(
                NodeAllocatorActor,
                "fixed",
                address,
                uid=NodeAllocatorActor.default_uid(),
                address=address,
            ),
            mo.create_actor(
                NodeInfoUploaderActor,
                NodeRole.WORKER,
                interval=kw.get("upload_interval"),
                band_to_resource=kw.get("band_to_resource"),
                use_gpu=kw.get("use_gpu", False),
                uid=NodeInfoUploaderActor.default_uid(),
                address=address,
            ),
            mo.create_actor(
                ProcessInfoManagerActor,
                uid=ProcessInfoManagerActor.default_uid(),
                address=address,
            ),
            mo.create_actor(
                FileLoggerActor, uid=FileLoggerActor.default_uid(), address=address
            ),
        ]
        dones, _ = await asyncio.wait(
            [asyncio.ensure_future(coro) for coro in create_actor_coros]
        )

        for task in dones:
            try:
                task.result()
            except mo.ActorAlreadyExist:  # pragma: no cover
                pass

        api = await super().create(address=address)
        await api.mark_node_ready()
        return api

    @classmethod
    async def cleanup(cls, address: str):
        from ..file_logger import FileLoggerActor
        from ..supervisor.locator import SupervisorPeerLocatorActor
        from ..supervisor.node_info import NodeInfoCollectorActor
        from ..uploader import NodeInfoUploaderActor

        await asyncio.gather(
            mo.destroy_actor(
                mo.create_actor_ref(
                    uid=SupervisorPeerLocatorActor.default_uid(), address=address
                )
            ),
            mo.destroy_actor(
                mo.create_actor_ref(
                    uid=NodeInfoCollectorActor.default_uid(), address=address
                )
            ),
            mo.destroy_actor(
                mo.create_actor_ref(
                    uid=NodeInfoUploaderActor.default_uid(), address=address
                )
            ),
            mo.destroy_actor(
                mo.create_actor_ref(uid=FileLoggerActor.default_uid(), address=address)
            ),
        )
