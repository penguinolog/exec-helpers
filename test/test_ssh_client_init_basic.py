#    Copyright 2018 Alexey Stepanov aka penguinolog.
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

from __future__ import absolute_import
from __future__ import unicode_literals

import typing  # noqa: F401

import mock
import paramiko
import pytest

import exec_helpers


def gen_private_keys(amount=1):  # type: (int) -> typing.List[paramiko.RSAKey]
    keys = []
    for _ in range(amount):
        keys.append(paramiko.RSAKey.generate(1024))
    return keys


def gen_public_key(private_key=None):  # type: (typing.Optional[paramiko.RSAKey]) -> str
    if private_key is None:
        private_key = paramiko.RSAKey.generate(1024)
    return "{0} {1}".format(private_key.get_name(), private_key.get_base64())


class FakeStream(object):
    def __init__(self, *args):
        self.__src = list(args)

    def __iter__(self):
        if len(self.__src) == 0:
            raise IOError()
        for _ in range(len(self.__src)):
            yield self.__src.pop(0)


host = "127.0.0.1"


configs = {
    "host_only": dict(host=host),
    "alternate_port": dict(host=host, port=2222),
    "username": dict(host=host, username="user"),
    "username_password": dict(host=host, username="user", password="password"),
    "username_password_empty_keys": dict(host=host, username="user", password="password", private_keys=[]),
    "username_single_key": dict(host=host, username="user", private_keys=gen_private_keys(1)),
    "username_password_single_key": dict(
        host=host, username="user", password="password", private_keys=gen_private_keys(1)
    ),
    "auth": dict(
        host=host, auth=exec_helpers.SSHAuth(username="user", password="password", key=gen_private_keys(1).pop())
    ),
    "auth_break": dict(
        host=host,
        username="Invalid",
        password="Invalid",
        private_keys=gen_private_keys(1),
        auth=exec_helpers.SSHAuth(username="user", password="password", key=gen_private_keys(1).pop()),
    ),
}


def pytest_generate_tests(metafunc):
    if "run_parameters" in metafunc.fixturenames:
        metafunc.parametrize(
            "run_parameters",
            [
                "host_only",
                "alternate_port",
                "username",
                "username_password",
                "username_password_empty_keys",
                "username_single_key",
                "username_password_single_key",
                "auth",
                "auth_break",
            ],
            indirect=True,
        )


@pytest.fixture
def run_parameters(request):
    return configs[request.param]


@pytest.fixture
def auto_add_policy(mocker):
    return mocker.patch("paramiko.AutoAddPolicy", return_value="AutoAddPolicy")


@pytest.fixture
def paramiko_ssh_client(mocker, run_parameters):
    mocker.patch("time.sleep")
    return mocker.patch("paramiko.SSHClient")


@pytest.fixture
def ssh_auth_logger(mocker):
    return mocker.patch("exec_helpers.ssh_auth.logger")


def teardown_function(function):
    """Clean-up after tests."""
    with mock.patch("warnings.warn"):
        exec_helpers.SSHClient._clear_cache()


def test_init_base(paramiko_ssh_client, auto_add_policy, run_parameters, ssh_auth_logger):
    # Helper code
    _ssh = mock.call
    port = run_parameters.get("port", 22)

    username = run_parameters.get("username", None)
    password = run_parameters.get("password", None)
    private_keys = run_parameters.get("private_keys", None)

    auth = run_parameters.get("auth", None)

    # Test
    ssh = exec_helpers.SSHClient(**run_parameters)

    paramiko_ssh_client.assert_called_once()
    auto_add_policy.assert_called_once()

    if auth is None:
        expected_calls = [
            _ssh.set_missing_host_key_policy("AutoAddPolicy"),
            _ssh.connect(hostname=host, password=password, pkey=None, port=port, username=username),
        ]

        assert expected_calls == paramiko_ssh_client().mock_calls

        assert ssh.auth == exec_helpers.SSHAuth(username=username, password=password, keys=private_keys)
    else:
        ssh_auth_logger.assert_not_called()

    sftp = ssh._sftp
    assert sftp == paramiko_ssh_client().open_sftp()
    assert ssh._ssh == paramiko_ssh_client()
    assert ssh.hostname == host
    assert ssh.port == port
    assert repr(ssh) == "{cls}(host={host}, port={port}, auth={auth!r})".format(
        cls=ssh.__class__.__name__, host=ssh.hostname, port=ssh.port, auth=ssh.auth
    )
