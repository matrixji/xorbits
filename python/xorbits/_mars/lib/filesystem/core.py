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

import glob as glob_
import os
from typing import Dict, List
from urllib.parse import urlparse

from ..compression import compress
from .base import FileSystem, path_type
from .local import LocalFileSystem
from .oss import OSSFileSystem

_filesystems = {"file": LocalFileSystem, "oss": OSSFileSystem}
_scheme_to_dependencies = {
    "hdfs": ["pyarrow"],
    "az": ["fsspec", "adlfs"],
    "abfs": ["fsspec", "adlfs"],
    "s3": ["fsspec", "s3fs"],
}


def register_filesystem(name: str, fs):
    _filesystems[name] = fs


def get_fs(path: path_type, storage_options: Dict = None) -> FileSystem:
    if storage_options is None:
        storage_options = dict()

    # detect scheme
    if os.path.exists(path) or glob_.glob(path):
        scheme = "file"
    else:
        scheme = urlparse(path).scheme
    if scheme == "" or len(scheme) == 1:  # len == 1 for windows
        scheme = "file"

    if scheme in _filesystems:
        file_system_type = _filesystems[scheme]
        if scheme == "file" or scheme == "oss":
            # local file systems are singletons.
            return file_system_type.get_instance()
        else:
            storage_options = file_system_type.get_storage_options(
                storage_options, path
            )
            return file_system_type(**storage_options)
    elif scheme in _scheme_to_dependencies:  # pragma: no cover
        dependencies = ", ".join(_scheme_to_dependencies[scheme])
        raise ImportError(f"Need to install {dependencies} to access {scheme}.")
    else:
        raise ValueError(
            f"Unknown file system type: {scheme}, "
            f'available include: {", ".join(_scheme_to_dependencies.keys())}'
        )


def glob(path: path_type, storage_options: Dict = None) -> List[path_type]:
    if "*" in path:
        fs = get_fs(path, storage_options)
        return fs.glob(path)
    else:
        return [path]


def file_size(path: path_type, storage_options: Dict = None) -> int:
    fs = get_fs(path, storage_options)
    return fs.stat(path)["size"]


def open_file(
    path: path_type,
    mode: str = "rb",
    compression: str = None,
    storage_options: Dict = None,
):
    fs = get_fs(path, storage_options)
    file = fs.open(path, mode=mode)

    if compression is not None:
        file = compress(file, compression)

    return file
