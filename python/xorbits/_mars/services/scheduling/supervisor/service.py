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

from .... import oscar as mo
from ...core import AbstractService
from .autoscale import AutoscalerActor
from .manager import DEFAULT_SUBTASK_MAX_RESCHEDULES


class SchedulingSupervisorService(AbstractService):
    """
    Scheduling service on supervisor.

    Scheduling Configuration
    ------------------------
    {
        "scheduling" : {
            "submit_period": 1,
            "autoscale" : {
                "enabled": false,
                "scheduler_backlog_timeout": 20,
                "sustained_scheduler_backlog_timeout": 20,
                "worker_idle_timeout": 40,
                "min_workers": 1,
                "max_workers": 100
            }
        }
    }
    """

    async def start(self):
        from .globalresource import GlobalResourceManagerActor

        await mo.create_actor(
            GlobalResourceManagerActor,
            uid=GlobalResourceManagerActor.default_uid(),
            address=self._address,
        )

        autoscale_config = self._config.get("scheduling", {}).get("autoscale", {})
        await mo.create_actor(
            AutoscalerActor,
            autoscale_config,
            uid=AutoscalerActor.default_uid(),
            address=self._address,
        )

    async def stop(self):
        from .autoscale import AutoscalerActor

        await mo.destroy_actor(
            mo.create_actor_ref(
                uid=AutoscalerActor.default_uid(), address=self._address
            )
        )

        from .globalresource import GlobalResourceManagerActor

        await mo.destroy_actor(
            mo.create_actor_ref(
                uid=GlobalResourceManagerActor.default_uid(), address=self._address
            )
        )

    async def create_session(self, session_id: str):
        service_config = self._config or dict()
        scheduling_config = service_config.get("scheduling", {})
        subtask_max_reschedules = scheduling_config.get(
            "subtask_max_reschedules", DEFAULT_SUBTASK_MAX_RESCHEDULES
        )
        subtask_cancel_timeout = scheduling_config.get("subtask_cancel_timeout", 5)
        speculation_config = scheduling_config.get("speculation", {})

        from .assigner import AssignerActor

        assigner_coro = mo.create_actor(
            AssignerActor,
            session_id,
            address=self._address,
            uid=AssignerActor.gen_uid(session_id),
        )

        from .queueing import SubtaskQueueingActor

        queueing_coro = mo.create_actor(
            SubtaskQueueingActor,
            session_id,
            scheduling_config.get("submit_period"),
            address=self._address,
            uid=SubtaskQueueingActor.gen_uid(session_id),
        )

        await asyncio.gather(assigner_coro, queueing_coro)

        from .manager import SubtaskManagerActor

        await mo.create_actor(
            SubtaskManagerActor,
            session_id,
            subtask_max_reschedules,
            subtask_cancel_timeout,
            speculation_config,
            address=self._address,
            uid=SubtaskManagerActor.gen_uid(session_id),
        )

        from ...cluster import ClusterAPI
        from .autoscale import AutoscalerActor

        cluster_api = await ClusterAPI.create(self._address)
        [autoscaler_ref] = await cluster_api.get_supervisor_refs(
            [AutoscalerActor.default_uid()]
        )
        await autoscaler_ref.register_session(session_id, self._address)

    async def destroy_session(self, session_id: str):
        from .assigner import AssignerActor
        from .autoscale import AutoscalerActor
        from .manager import SubtaskManagerActor
        from .queueing import SubtaskQueueingActor

        autoscaler_ref = await mo.actor_ref(
            AutoscalerActor.default_uid(), address=self._address
        )
        await autoscaler_ref.unregister_session(session_id)

        destroy_tasks = []
        for actor_cls in [SubtaskManagerActor, SubtaskQueueingActor, AssignerActor]:
            ref = await mo.actor_ref(
                actor_cls.gen_uid(session_id), address=self._address
            )
            destroy_tasks.append(asyncio.create_task(ref.destroy()))
        await asyncio.gather(*destroy_tasks)
