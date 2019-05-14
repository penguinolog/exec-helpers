#    Copyright 2018 - 2019 Alexey Stepanov aka penguinolog

#    Copyright 2016 Mirantis, Inc.

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

"""Execution helpers for simplified usage of subprocess and ssh."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

# Standard Library
import ast
import collections
import distutils.errors
import os.path
import shutil
import sys
from distutils.command import build_ext

# External Dependencies
import setuptools

try:
    # noinspection PyPackageRequirements
    from Cython.Build import cythonize
except ImportError:
    cythonize = None

with open(os.path.join(os.path.dirname(__file__), "exec_helpers", "__init__.py")) as f:
    SOURCE = f.read()

with open("requirements.txt") as f:
    REQUIRED = f.read().splitlines()

with open("README.rst") as f:
    LONG_DESCRIPTION = f.read()


def _extension(modpath):
    """Make setuptools.Extension."""
    source_path = modpath.replace(".", "/") + ".py"
    return setuptools.Extension(modpath, [source_path])


REQUIRES_OPTIMIZATION = [
    _extension("exec_helpers._log_templates"),
    _extension("exec_helpers._ssh_client_base"),
    _extension("exec_helpers._subprocess_helpers"),

    _extension("exec_helpers.api"),
    _extension("exec_helpers.constants"),
    _extension("exec_helpers.exceptions"),
    _extension("exec_helpers.exec_result"),
    _extension("exec_helpers.proc_enums"),
    _extension("exec_helpers.ssh_auth"),
    _extension("exec_helpers.ssh_client"),
    _extension("exec_helpers.subprocess_runner"),
]

if "win32" != sys.platform:
    REQUIRES_OPTIMIZATION.append(_extension("exec_helpers.__init__"))

# noinspection PyCallingNonCallable
EXT_MODULES = (
    cythonize(
        REQUIRES_OPTIMIZATION,
        compiler_directives=dict(
            always_allow_keywords=True, binding=True, embedsignature=True, overflowcheck=True, language_level=3
        ),
    )
    if cythonize is not None
    else []
)


class BuildFailed(Exception):
    """For install clear scripts."""


class AllowFailRepair(build_ext.build_ext):
    """This class allows C extension building to fail and repairs init."""

    def run(self):
        """Run.

        :raises BuildFailed: Build is failed and clean python code should be used.
        """
        try:
            build_ext.build_ext.run(self)

            # Copy __init__.py back to repair package.
            build_dir = os.path.abspath(self.build_lib)
            root_dir = os.path.abspath(os.path.join(__file__, ".."))
            target_dir = build_dir if not self.inplace else root_dir

            src_files = (os.path.join("exec_helpers", "__init__.py"),)

            for src_file in src_files:
                src = os.path.join(root_dir, src_file)
                dst = os.path.join(target_dir, src_file)

                if src != dst:
                    shutil.copyfile(src, dst)
        except (
            distutils.errors.DistutilsPlatformError,
            getattr(globals()["__builtins__"], "FileNotFoundError", OSError),
        ):
            raise BuildFailed()

    def build_extension(self, ext):
        """build_extension.

        :raises BuildFailed: Build is failed and clean python code should be used.
        """
        try:
            build_ext.build_ext.build_extension(self, ext)
        except (
            distutils.errors.CCompilerError,
            distutils.errors.DistutilsExecError,
            distutils.errors.DistutilsPlatformError,
            ValueError,
        ):
            raise BuildFailed()


# noinspection PyUnresolvedReferences
def get_simple_vars_from_src(src):
    """Get simple (string/number/boolean and None) assigned values from source.

    :param src: Source code
    :type src: str
    :returns: OrderedDict with keys, values = variable names, values
    :rtype: typing.Dict[
                str,
                typing.Union[
                    str, bytes,
                    int, float, complex,
                    list, set, dict, tuple,
                    None, bool, Ellipsis
                ]
            ]

    Limitations: Only defined from scratch variables.
    Not supported by design:
        * Imports
        * Executable code, including string formatting and comprehensions.

    Examples:

    >>> string_sample = "a = '1'"
    >>> get_simple_vars_from_src(string_sample)
    OrderedDict([('a', '1')])

    >>> int_sample = "b = 1"
    >>> get_simple_vars_from_src(int_sample)
    OrderedDict([('b', 1)])

    >>> list_sample = "c = [u'1', b'1', 1, 1.0, 1j, None]"
    >>> result = get_simple_vars_from_src(list_sample)
    >>> result == collections.OrderedDict(
    ...     [('c', [u'1', b'1', 1, 1.0, 1j, None])]
    ... )
    True

    >>> iterable_sample = "d = ([1], {1: 1}, {1})"
    >>> get_simple_vars_from_src(iterable_sample)
    OrderedDict([('d', ([1], {1: 1}, {1}))])

    >>> multiple_assign = "e = f = g = 1"
    >>> get_simple_vars_from_src(multiple_assign)
    OrderedDict([('e', 1), ('f', 1), ('g', 1)])
    """
    if sys.version_info[:2] < (3, 8):
        ast_data = (ast.Str, ast.Num, ast.List, ast.Set, ast.Dict, ast.Tuple, ast.Bytes, ast.NameConstant, ast.Ellipsis)
    else:
        ast_data = ast.Constant

    tree = ast.parse(src)

    result = collections.OrderedDict()

    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign):  # We parse assigns only
            continue
        try:
            if isinstance(node.value, ast_data):
                value = ast.literal_eval(node.value)
            else:
                continue
        except ValueError:
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and isinstance(tgt.ctx, ast.Store):
                result[tgt.id] = value
    return result


VARIABLES = get_simple_vars_from_src(SOURCE)

CLASSIFIERS = [
    "Development Status :: 5 - Production/Stable",
    "Intended Audience :: Developers",
    "Topic :: Software Development :: Libraries :: Python Modules",
    "License :: OSI Approved :: Apache Software License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.4",
    "Programming Language :: Python :: 3.5",
    "Programming Language :: Python :: 3.6",
    "Programming Language :: Python :: 3.7",
    "Programming Language :: Python :: 3.8",
    "Programming Language :: Python :: Implementation :: CPython",
    "Programming Language :: Python :: Implementation :: PyPy",
]

KEYWORDS = ["logging", "debugging", "development"]

setup_args = dict(
    name="exec-helpers",
    author=VARIABLES["__author__"],
    author_email=VARIABLES["__author_email__"],
    maintainer=", ".join(
        "{name} <{email}>".format(name=name, email=email) for name, email in VARIABLES["__maintainers__"].items()
    ),
    url=VARIABLES["__url__"],
    license=VARIABLES["__license__"],
    description=VARIABLES["__description__"],
    long_description=LONG_DESCRIPTION,
    classifiers=CLASSIFIERS,
    keywords=KEYWORDS,
    python_requires=">=3.4",
    # While setuptools cannot deal with pre-installed incompatible versions,
    # setting a lower bound is not harmful - it makes error messages cleaner. DO
    # NOT set an upper bound on setuptools, as that will lead to uninstallable
    # situations as progressive releases of projects are done.
    # Blacklist setuptools 34.0.0-34.3.2 due to https://github.com/pypa/setuptools/issues/951
    # Blacklist setuptools 36.2.0 due to https://github.com/pypa/setuptools/issues/1086
    setup_requires=[
        "setuptools >= 21.0.0,!=24.0.0,"
        "!=34.0.0,!=34.0.1,!=34.0.2,!=34.0.3,!=34.1.0,!=34.1.1,!=34.2.0,!=34.3.0,!=34.3.1,!=34.3.2,"
        "!=36.2.0",
        "setuptools_scm",
    ],
    use_scm_version=True,
    install_requires=REQUIRED,
    package_data={"exec_helpers": ["py.typed"]},
)
if cythonize is not None:
    setup_args["ext_modules"] = EXT_MODULES
    setup_args["cmdclass"] = dict(build_ext=AllowFailRepair)

try:
    setuptools.setup(**setup_args)
except BuildFailed:
    print("*" * 80 + "\n" "* Build Failed!\n" "* Use clear scripts version.\n" "*" * 80 + "\n")
    del setup_args["ext_modules"]
    del setup_args["cmdclass"]
    setuptools.setup(**setup_args)
