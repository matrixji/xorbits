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

import argparse
import asyncio
import glob
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from concurrent import futures
from typing import List

import numpy as np
import psutil
import pytest

from .... import tensor as mt
from ....lib.aio import get_isolation, new_isolation, stop_isolation
from ....services import NodeRole
from ....services.cluster import ClusterAPI
from ....session import new_session
from ....tests import flaky
from ....utils import clean_mars_tmp_dir, get_next_port
from ..cmdline import OscarCommandRunner
from ..supervisor import SupervisorCommandRunner
from ..worker import WorkerCommandRunner

logger = logging.getLogger(__name__)


class _ProcessExitedException(Exception):
    pass


def _wait_supervisor_ready(supervisor_proc: subprocess.Popen, timeout=120):
    start_time = time.time()
    supervisor_pid = supervisor_proc.pid
    while True:
        if supervisor_proc.poll() is not None:
            raise _ProcessExitedException

        try:
            ep_file_name = OscarCommandRunner._build_endpoint_file_path(
                pid=supervisor_pid
            )
            with open(ep_file_name, "r") as ep_file:
                return ep_file.read().strip()
        except:  # noqa: E722  # pylint: disable=bare-except
            if time.time() - start_time > timeout:
                raise
            pass
        finally:
            time.sleep(0.1)


def _wait_worker_ready(
    supervisor_addr, worker_procs: List[subprocess.Popen], n_supervisors=1, timeout=30
):
    async def wait_for_workers():
        start_time = time.time()
        while True:
            if any(proc.poll() is not None for proc in worker_procs):
                raise _ProcessExitedException

            try:
                cluster_api = await ClusterAPI.create(supervisor_addr)
                sv_info = await cluster_api.get_nodes_info(
                    role=NodeRole.SUPERVISOR, resource=True
                )
                worker_info = await cluster_api.get_nodes_info(
                    role=NodeRole.WORKER, resource=True
                )
                if len(sv_info) >= n_supervisors and len(worker_info) >= len(
                    worker_procs
                ):
                    break

                logger.info(
                    "Cluster not satisfied. sv_num=%s worker_num=%s",
                    len(sv_info),
                    len(worker_info),
                )
            except:  # noqa: E722  # pylint: disable=bare-except
                logger.exception("Error when waiting for workers to start")
                if time.time() - start_time > timeout:
                    raise
                pass
            finally:
                await asyncio.sleep(0.5)

    isolation = get_isolation()
    asyncio.run_coroutine_threadsafe(wait_for_workers(), isolation.loop).result(timeout)


_test_port_cache = dict()


def _get_labelled_port(label=None, create=True):
    test_name = os.environ["PYTEST_CURRENT_TEST"]
    if (test_name, label) not in _test_port_cache:
        if create:
            _test_port_cache[(test_name, label)] = get_next_port(occupy=True)
        else:
            return None
    return _test_port_cache[(test_name, label)]


def _stop_processes(procs: List[subprocess.Popen]):
    sub_ps_procs = []
    for proc in procs:
        if not proc:
            continue

        sub_ps_procs.extend(psutil.Process(proc.pid).children(recursive=True))
        proc.terminate()

    for proc in procs:
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            pass

    for ps_proc in sub_ps_procs + procs:
        try:
            ps_proc.kill()
        except psutil.NoSuchProcess:
            pass


supervisor_cmd_start = [sys.executable, "-m", "xorbits._mars.deploy.oscar.supervisor"]
worker_cmd_start = [sys.executable, "-m", "xorbits._mars.deploy.oscar.worker"]


def _reload_args(args):
    return [arg if not callable(arg) else arg() for arg in args]


_rerun_errors = (
    _ProcessExitedException,
    asyncio.TimeoutError,
    futures.TimeoutError,
    OSError,
    TimeoutError,
)


@flaky(max_runs=10, rerun_filter=lambda err, *_: issubclass(err[0], _rerun_errors))
@pytest.mark.parametrize(
    "supervisor_args,worker_args,use_web_addr",
    [
        pytest.param(
            supervisor_cmd_start,
            worker_cmd_start
            + [
                "--config-file",
                os.path.join(os.path.dirname(__file__), "local_test_config.yml"),
            ],
            False,
            id="bare_start",
        ),
        pytest.param(
            supervisor_cmd_start
            + [
                "-e",
                lambda: f'127.0.0.1:{_get_labelled_port("supervisor")}',
                "-w",
                lambda: str(_get_labelled_port("web")),
                "--n-process=2",
                "--log-level=DEBUG",
            ],
            worker_cmd_start
            + [
                "-e",
                lambda: f"127.0.0.1:{get_next_port(occupy=True)}",
                "-s",
                lambda: f'127.0.0.1:{_get_labelled_port("supervisor")}',
                "--config-file",
                os.path.join(os.path.dirname(__file__), "local_test_config.yml"),
                "--log-level=DEBUG",
                "--log-format=%(asctime)s %(message)s",
                "--use-uvloop=no",
            ],
            True,
            id="with_supervisors",
        ),
    ],
)
def test_cmdline_run(supervisor_args, worker_args, use_web_addr):
    new_isolation()
    sv_proc = w_procs = None
    restart_trial = 5
    try:
        env = os.environ.copy()
        env["MARS_CPU_TOTAL"] = "2"

        for trial in range(restart_trial):
            logger.warning("Cluster start attempt %d / %d", trial + 1, restart_trial)
            _test_port_cache.clear()

            sv_args = _reload_args(supervisor_args)
            sv_proc = subprocess.Popen(sv_args, env=env)

            oscar_port = _get_labelled_port("supervisor", create=False)
            if not oscar_port:
                oscar_ep = _wait_supervisor_ready(sv_proc)
            else:
                oscar_ep = f"127.0.0.1:{oscar_port}"

            if use_web_addr:
                host = oscar_ep.rsplit(":", 1)[0]
                api_ep = f'http://{host}:{_get_labelled_port("web", create=False)}'
            else:
                api_ep = oscar_ep

            w_procs = []
            for idx in range(2):
                proc = subprocess.Popen(_reload_args(worker_args), env=env)
                w_procs.append(proc)
                # make sure worker ports does not collide
                time.sleep(2)

            try:
                _wait_worker_ready(oscar_ep, w_procs)
                break
            except (asyncio.TimeoutError, futures.TimeoutError, TimeoutError):
                if trial == restart_trial - 1:
                    raise
                else:
                    _stop_processes(w_procs + [sv_proc])

        new_session(api_ep)
        data = np.random.rand(10, 10)
        res = mt.tensor(data, chunk_size=5).sum().execute().fetch()
        np.testing.assert_almost_equal(res, data.sum())
    finally:
        stop_isolation()

        ep_file_name = OscarCommandRunner._build_endpoint_file_path(pid=sv_proc.pid)
        try:
            os.unlink(ep_file_name)
        except OSError:
            pass

        _stop_processes((w_procs or []) + [sv_proc])

        port_prefix = os.path.join(
            tempfile.gettempdir(), OscarCommandRunner._port_file_prefix
        )
        for fn in glob.glob(port_prefix + "*"):
            os.unlink(fn)


def test_parse_args():
    parser = argparse.ArgumentParser(description="TestService")
    app = WorkerCommandRunner()
    app.config_args(parser)

    task_detail = """
    {
      "cluster": {
        "supervisor": ["sv1", "sv2"],
        "worker": ["worker1", "worker2"]
      },
      "task": {
        "type": "worker",
        "index": 0
      }
    }
    """

    env = {
        "MARS_LOAD_MODULES": "extra.module",
        "MARS_TASK_DETAIL": task_detail,
        "MARS_CACHE_MEM_SIZE": "20M",
        "MARS_PLASMA_DIRS": "/dev/shm",
        "MARS_SPILL_DIRS": "/tmp",
    }
    args = app.parse_args(parser, ["-p", "10324"], env)
    assert args.host == "worker1"
    assert args.endpoint == "worker1:10324"
    assert args.supervisors == "sv1,sv2"
    assert "extra.module" in args.load_modules
    assert app.config["storage"]["plasma"] == {
        "store_memory": "20M",
        "plasma_directory": "/dev/shm",
    }
    assert app.config["storage"]["disk"] == {
        "root_dirs": "/tmp",
    }


@pytest.fixture
def init_app():
    parser = argparse.ArgumentParser(description="TestService")
    app = WorkerCommandRunner()
    app.config_args(parser)
    yield app, parser

    # clean
    clean_mars_tmp_dir()


def test_parse_no_log_dir(init_app):
    app, parser = init_app

    assert not app.config
    assert len(app.config) == 0

    with pytest.raises(KeyError):
        try:
            app._set_log_dir()
        except ValueError:
            pytest.fail()

    _ = app.parse_args(parser, ["--supervisors", "127.0.0.1"])
    assert app.config["cluster"]
    assert not app.config["cluster"]["log_dir"]
    app._set_log_dir()
    assert app.logging_conf["from_cmd"] is True
    assert not app.logging_conf["log_dir"]


def test_parse_log_dir(init_app):
    app, parser = init_app
    log_dir = tempfile.mkdtemp()
    _ = app.parse_args(parser, ["--supervisors", "127.0.0.1"])
    app.config["cluster"]["log_dir"] = log_dir
    assert os.path.exists(app.config["cluster"]["log_dir"])
    app._set_log_dir()
    assert app.logging_conf["log_dir"] == log_dir


def test_config_logging(init_app):
    app, parser = init_app
    app.args = app.parse_args(parser, ["--supervisors", "127.0.0.1"])
    app.config_logging()
    expected_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "file-logging.conf"
    )
    assert app.logging_conf["file"] == expected_path


def test_parse_third_party_modules():
    config = {
        "third_party_modules": {
            "supervisor": ["supervisor.module"],
            "worker": ["worker.module"],
        }
    }
    env = {"MARS_LOAD_MODULES": "extra.module"}

    parser = argparse.ArgumentParser(description="TestService")
    app = WorkerCommandRunner()
    app.config_args(parser)
    args = app.parse_args(
        parser,
        [
            "-c",
            json.dumps(config),
            "-p",
            "10324",
            "-s",
            "sv1,sv2",
            "--load-modules",
            "load.module",
        ],
        env,
    )
    assert args.load_modules == ("load.module", "worker.module", "extra.module")

    parser = argparse.ArgumentParser(description="TestService")
    app = SupervisorCommandRunner()
    app.config_args(parser)
    args = app.parse_args(
        parser,
        [
            "-c",
            json.dumps(config),
            "-p",
            "10324",
            "-s",
            "sv1,sv2",
            "--load-modules",
            "load.module",
        ],
        env,
    )
    assert args.load_modules == ("load.module", "supervisor.module", "extra.module")
