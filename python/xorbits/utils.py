# Copyright 2022-2023 XProbe Inc.
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

import importlib
import sys
import traceback
from typing import Callable, Optional


def is_pydev_evaluating_value() -> bool:
    for frame in traceback.extract_stack():
        if "pydev" in frame.filename and frame.name == "var_to_xml":
            return True
    return False


def safe_repr_str(f: Callable):
    def inn(self, *args, **kwargs):
        if is_pydev_evaluating_value():
            # if is evaluating value from pydev, pycharm, etc
            # skip repr or str
            return getattr(object, f.__name__)(self)
        else:
            return f(self, *args, **kwargs)

    return inn


def get_local_py_version():
    """
    Get the python version on the machine where Xorbits is installed, formatted by "major.minor", like "3.10"
    """
    return str(sys.version_info.major) + "." + str(sys.version_info.minor)


def get_local_package_version(package_name: str) -> Optional[str]:
    """
    Get the version of a python package. If the package is not installed, return None
    """
    try:
        return importlib.import_module(package_name).__version__
    except ModuleNotFoundError:
        return None
