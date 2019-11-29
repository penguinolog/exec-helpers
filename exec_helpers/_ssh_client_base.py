#    Copyright 2018 - 2019 Alexey Stepanov aka penguinolog.

#    Copyright 2013 - 2016 Mirantis, Inc.
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

"""SSH client helper based on Paramiko. Base class."""

__all__ = ("SSHClientBase", "SshExecuteAsyncResult")

# Standard Library
import concurrent.futures
import copy
import datetime
import logging
import shlex
import socket
import stat
import time
import typing
import warnings

# External Dependencies
import paramiko  # type: ignore
import tenacity  # type: ignore
import threaded

# Exec-Helpers Implementation
from exec_helpers import _log_templates
from exec_helpers import _ssh_helpers
from exec_helpers import api
from exec_helpers import constants
from exec_helpers import exceptions
from exec_helpers import exec_result
from exec_helpers import proc_enums
from exec_helpers import ssh_auth

# Local Implementation
from ._ssh_helpers import HostsSSHConfigs
from ._ssh_helpers import SSHConfig

logging.getLogger("paramiko").setLevel(logging.WARNING)


class RetryOnExceptions(tenacity.retry_if_exception):  # type: ignore
    """Advanced retry on exceptions."""

    def __init__(
        self,
        retry_on: "typing.Union[typing.Type[BaseException], typing.Tuple[typing.Type[BaseException], ...]]",
        reraise: "typing.Union[typing.Type[BaseException], typing.Tuple[typing.Type[BaseException], ...]]",
    ) -> None:
        """Retry on exceptions, except several types.

        :param retry_on: Exceptions to retry on
        :param reraise: Exceptions, which should be reraised, even if subclasses retry_on
        """
        super(RetryOnExceptions, self).__init__(lambda e: isinstance(e, retry_on) and not isinstance(e, reraise))


# noinspection PyTypeHints
class SshExecuteAsyncResult(api.ExecuteAsyncResult):
    """Override original NamedTuple with proper typing."""

    @property
    def interface(self) -> paramiko.Channel:
        """Override original NamedTuple with proper typing."""
        return super(SshExecuteAsyncResult, self).interface

    @property
    def stdin(self) -> paramiko.ChannelFile:  # type: ignore
        """Override original NamedTuple with proper typing."""
        return super(SshExecuteAsyncResult, self).stdin

    @property
    def stderr(self) -> typing.Optional[paramiko.ChannelFile]:  # type: ignore
        """Override original NamedTuple with proper typing."""
        return super(SshExecuteAsyncResult, self).stderr

    @property
    def stdout(self) -> typing.Optional[paramiko.ChannelFile]:  # type: ignore
        """Override original NamedTuple with proper typing."""
        return super(SshExecuteAsyncResult, self).stdout


class _SudoContext:
    """Context manager for call commands with sudo."""

    __slots__ = ("__ssh", "__sudo_status", "__enforce")

    def __init__(self, ssh: "SSHClientBase", enforce: typing.Optional[bool] = None) -> None:
        """Context manager for call commands with sudo.

        :param ssh: connection instance
        :type ssh: SSHClientBase
        :param enforce: sudo mode for context manager
        :type enforce: typing.Optional[bool]
        """
        self.__ssh: "SSHClientBase" = ssh
        self.__sudo_status: bool = ssh.sudo_mode
        self.__enforce: typing.Optional[bool] = enforce

    def __enter__(self) -> None:
        self.__sudo_status = self.__ssh.sudo_mode
        if self.__enforce is not None:
            self.__ssh.sudo_mode = self.__enforce

    def __exit__(self, exc_type: typing.Any, exc_val: typing.Any, exc_tb: typing.Any) -> None:
        self.__ssh.sudo_mode = self.__sudo_status


class _KeepAliveContext:
    """Context manager for keepalive management."""

    __slots__ = ("__ssh", "__keepalive_status", "__enforce")

    def __init__(self, ssh: "SSHClientBase", enforce: int) -> None:
        """Context manager for keepalive management.

        :param ssh: connection instance
        :type ssh: SSHClientBase
        :param enforce: keepalive period for context manager
        :type enforce: int
        """
        self.__ssh: "SSHClientBase" = ssh
        self.__keepalive_status: int = ssh.keepalive_mode
        self.__enforce: int = enforce

    def __enter__(self) -> None:
        self.__ssh.__enter__()
        self.__keepalive_status = self.__ssh.keepalive_mode
        self.__ssh.keepalive_mode = self.__enforce

    def __exit__(self, exc_type: typing.Any, exc_val: typing.Any, exc_tb: typing.Any) -> None:
        # Exit before releasing!
        self.__ssh.__exit__(exc_type=exc_type, exc_val=exc_val, exc_tb=exc_tb)  # type: ignore
        self.__ssh.keepalive_mode = self.__keepalive_status


class SSHClientBase(api.ExecHelper):
    """SSH Client helper."""

    __slots__ = (
        "__hostname",
        "__port",
        "__auth_mapping",
        "__ssh",
        "__sftp",
        "__sudo_mode",
        "__keepalive_period",
        "__verbose",
        "__ssh_config",
        "__sock",
        "__conn_chain",
    )

    def __hash__(self) -> int:
        """Hash for usage as dict keys."""
        return hash((self.__class__, self.hostname, self.port, self.auth))

    def __init__(
        self,
        host: str,
        port: typing.Optional[int] = None,
        username: typing.Optional[str] = None,
        password: typing.Optional[str] = None,
        *,
        private_keys: typing.Optional[typing.Sequence[paramiko.RSAKey]] = None,
        auth: typing.Optional[ssh_auth.SSHAuth] = None,
        verbose: bool = True,
        ssh_config: typing.Union[
            str,
            paramiko.SSHConfig,
            typing.Dict[str, typing.Dict[str, typing.Union[str, int, bool, typing.List[str]]]],
            HostsSSHConfigs,
            None,
        ] = None,
        ssh_auth_map: typing.Optional[typing.Union[typing.Dict[str, ssh_auth.SSHAuth], ssh_auth.SSHAuthMapping]] = None,
        sock: typing.Optional[typing.Union[paramiko.ProxyCommand, paramiko.Channel, socket.socket]] = None,
        keepalive: typing.Union[int, bool] = 1,
    ) -> None:
        """Main SSH Client helper.

        :param host: remote hostname
        :type host: str
        :param port: remote ssh port
        :type port: typing.Optional[int]
        :param username: remote username.
        :type username: typing.Optional[str]
        :param password: remote password
        :type password: typing.Optional[str]
        :param private_keys: private keys for connection
        :type private_keys: typing.Optional[typing.Sequence[paramiko.RSAKey]]
        :param auth: credentials for connection
        :type auth: typing.Optional[ssh_auth.SSHAuth]
        :param verbose: show additional error/warning messages
        :type verbose: bool
        :param ssh_config: SSH configuration for connection. Maybe config path, parsed as dict and paramiko parsed.
        :type ssh_config:
            typing.Union[
                str,
                paramiko.SSHConfig,
                typing.Dict[str, typing.Dict[str, typing.Union[str, int, bool, typing.List[str]]]],
                HostsSSHConfigs,
                None
            ]
        :param ssh_auth_map: SSH authentication information mapped to host names. Useful for complex SSH Proxy cases.
        :type ssh_auth_map: typing.Optional[typing.Union[typing.Dict[str, ssh_auth.SSHAuth], ssh_auth.SSHAuthMapping]]
        :param sock: socket for connection. Useful for ssh proxies support
        :type sock: typing.Optional[typing.Union[paramiko.ProxyCommand, paramiko.Channel, socket.socket]]
        :param keepalive: keepalive period
        :type keepalive: typing.Union[int, bool]

        .. note:: auth has priority over username/password/private_keys
        .. note::

            for proxy connection auth information is collected from SSHConfig
            if ssh_auth_map record is not available

        .. versionchanged:: 6.0.0 private_keys, auth and vebose became keyword-only arguments
        .. versionchanged:: 6.0.0 added optional ssh_config for ssh-proxy & low level connection parameters handling
        .. versionchanged:: 6.0.0 added optional ssh_auth_map for ssh proxy cases with authentication on each step
        .. versionchanged:: 6.0.0 added optional sock for manual proxy chain handling
        .. versionchanged:: 6.0.0 keepalive exposed to constructor
        .. versionchanged:: 6.0.0 keepalive became int, now used in ssh transport as period of keepalive requests
        .. versionchanged:: 6.0.0 private_keys is deprecated
        """
        if private_keys is not None:
            warnings.warn(
                "private_keys setting without SSHAuth object is deprecated and will be removed at short time.",
                DeprecationWarning,
            )

        # Init ssh config. It's main source for connection parameters
        if isinstance(ssh_config, HostsSSHConfigs):
            self.__ssh_config: HostsSSHConfigs = ssh_config
        else:
            self.__ssh_config = _ssh_helpers.parse_ssh_config(ssh_config, host)

        # Get config. We are not resolving full chain. If you are have a chain by some reason - init config manually.
        config: SSHConfig = self.__ssh_config[host]

        # Save resolved hostname and port
        self.__hostname: str = config.hostname
        self.__port: int = port if port is not None else config.port if config.port is not None else 22

        # Store initial auth mapping
        self.__auth_mapping = ssh_auth.SSHAuthMapping(ssh_auth_map)
        # We are already resolved hostname
        if self.hostname not in self.__auth_mapping and host in self.__auth_mapping:
            self.__auth_mapping[self.hostname] = self.__auth_mapping[host]

        self.__sudo_mode = False
        self.__keepalive_period: int = int(keepalive)
        self.__verbose: bool = verbose
        self.__sock = sock

        self.__ssh: paramiko.SSHClient
        self.__sftp: typing.Optional[paramiko.SFTPClient] = None

        # Rebuild SSHAuth object if required.
        # Priority: auth > credentials > auth mapping
        if auth is not None:
            self.__auth_mapping[self.hostname] = real_auth = copy.copy(auth)
        elif self.hostname not in self.__auth_mapping or any((username, password, private_keys)):
            self.__auth_mapping[self.hostname] = real_auth = ssh_auth.SSHAuth(
                username=username if username is not None else config.user,
                password=password,
                keys=private_keys,
                key_filename=config.identityfile,
            )
        else:
            real_auth = self.__auth_mapping[self.hostname]

        # Init super with host and real port and username
        mod_name = "exec_helpers" if self.__module__.startswith("exec_helpers") else self.__module__
        super(SSHClientBase, self).__init__(
            logger=logging.getLogger(f"{mod_name}.{self.__class__.__name__}").getChild(
                f"({real_auth.username}@{host}:{self.__port})"
            )
        )

        # Update config for target host: merge with data from credentials and parameters.
        # SSHConfig is the single source for hostname/port/... during low level connection construction.
        self.__rebuild_ssh_config()

        # Build connection chain once and use it for connection later
        if sock is None:
            self.__conn_chain: typing.List[typing.Tuple[SSHConfig, ssh_auth.SSHAuth]] = self.__build_connection_chain()
        else:
            self.__conn_chain = []

        self.__connect()

    def __rebuild_ssh_config(self) -> None:
        """Rebuild main ssh config from available information."""
        self.__ssh_config[self.hostname] = self.__ssh_config[self.hostname].overridden_by(
            _ssh_helpers.SSHConfig(
                hostname=self.hostname, port=self.port, user=self.auth.username, identityfile=self.auth.key_filename,
            )
        )

    def __build_connection_chain(self) -> typing.List[typing.Tuple[SSHConfig, ssh_auth.SSHAuth]]:
        conn_chain: typing.List[typing.Tuple[SSHConfig, ssh_auth.SSHAuth]] = []

        config = self.ssh_config[self.hostname]
        default_auth = ssh_auth.SSHAuth(username=config.user, key_filename=config.identityfile)
        auth = self.__auth_mapping.get_with_alt_hostname(config.hostname, self.hostname, default=default_auth)
        conn_chain.append((config, auth))

        while config.proxyjump is not None:
            # pylint: disable=no-member
            config = self.ssh_config[config.proxyjump]
            default_auth = ssh_auth.SSHAuth(username=config.user, key_filename=config.identityfile)
            conn_chain.append((config, self.__auth_mapping.get(config.hostname, default_auth)))
        return conn_chain[::-1]

    @property
    def auth(self) -> ssh_auth.SSHAuth:
        """Internal authorisation object.

        Attention: this public property is mainly for inheritance,
        debug and information purposes.
        Calls outside SSHClient and child classes is sign of incorrect design.
        Change is completely disallowed.

        :rtype: ssh_auth.SSHAuth
        """
        return self.__auth_mapping[self.hostname]

    @property
    def hostname(self) -> str:
        """Connected remote host name.

        :rtype: str
        """
        return self.__hostname

    @property
    def port(self) -> int:
        """Connected remote port number.

        :rtype: int
        """
        return self.__port

    @property
    def ssh_config(self) -> HostsSSHConfigs:
        """SSH connection config.

        :rtype: HostsSSHConfigs
        """
        return copy.deepcopy(self.__ssh_config)

    @property
    def is_alive(self) -> bool:
        """Paramiko status: ready to use|reconnect required.

        :rtype: bool
        """
        return self.__ssh.get_transport() is not None

    def __repr__(self) -> str:
        """Representation for debug purposes."""
        return f"{self.__class__.__name__}(host={self.hostname}, port={self.port}, auth={self.auth!r})"

    def __str__(self) -> str:  # pragma: no cover
        """Representation for debug purposes."""
        return f"{self.__class__.__name__}(host={self.hostname}, port={self.port}) " f"for user {self.auth.username}"

    @property
    def _ssh(self) -> paramiko.SSHClient:
        """Ssh client object getter for inheritance support only.

        Attention: ssh client object creation and change
        is allowed only by __init__ and reconnect call.

        :rtype: paramiko.SSHClient
        """
        return self.__ssh

    @tenacity.retry(  # type: ignore
        retry=RetryOnExceptions(retry_on=paramiko.SSHException, reraise=paramiko.AuthenticationException),
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_fixed(3),
        reraise=True,
    )
    def __connect(self) -> None:
        """Main method for connection open."""
        with self.lock:
            if self.__sock is not None:
                sock = self.__sock

                self.__ssh = paramiko.SSHClient()
                self.__ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self.auth.connect(
                    client=self.__ssh, hostname=self.hostname, port=self.port, log=self.__verbose, sock=sock,
                )
            else:
                self.__ssh = self.__get_client()

            transport: paramiko.Transport = self.__ssh.get_transport()
            transport.set_keepalive(1 if self.__keepalive_period else 0)  # send keepalive packets

    def __get_client(self) -> paramiko.SSHClient:
        """Connect using connection chain information.

        :return: paramiko ssh connection object
        :rtype: paramiko.SSHClient
        :raises ValueError: ProxyCommand found in connection chain after first host reached
        :raises RuntimeError: Unexpected state
        """

        last_ssh_client: paramiko.SSHClient = paramiko.SSHClient()
        last_ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        config, auth = self.__conn_chain[0]
        if config.proxycommand:
            auth.connect(
                last_ssh_client,
                hostname=config.hostname,
                port=config.port or 22,
                sock=paramiko.ProxyCommand(config.proxycommand),
            )
        else:
            auth.connect(last_ssh_client, hostname=config.hostname, port=config.port or 22)

        for config, auth in self.__conn_chain[1:]:  # start has another logic, so do it out of cycle
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            if config.proxyjump:
                sock = last_ssh_client.get_transport().open_channel(
                    kind="direct-tcpip", dest_addr=(config.hostname, config.port or 22), src_addr=(config.proxyjump, 0),
                )
                auth.connect(ssh, hostname=config.hostname, port=config.port or 22, sock=sock)
                last_ssh_client = ssh
                continue

            if config.proxycommand:
                raise ValueError(f"ProxyCommand found in connection chain after first host reached!\n{config}")

            raise RuntimeError(f"Unexpected state: Final host by configuration, but requested host is not reached")
        return last_ssh_client

    def __connect_sftp(self) -> None:
        """SFTP connection opener."""
        with self.lock:
            try:
                self.__sftp = self.__ssh.open_sftp()
            except paramiko.SSHException:
                self.logger.warning("SFTP enable failed! SSH only is accessible.")

    @property
    def _sftp(self) -> paramiko.sftp_client.SFTPClient:
        """SFTP channel access for inheritance.

        :rtype: paramiko.sftp_client.SFTPClient
        :raises paramiko.SSHException: SFTP connection failed
        """
        if self.__sftp is not None:
            return self.__sftp
        self.logger.debug("SFTP is not connected, try to connect...")
        self.__connect_sftp()
        if self.__sftp is not None:
            return self.__sftp
        raise paramiko.SSHException("SFTP connection failed")

    def close(self) -> None:
        """Close SSH and SFTP sessions."""
        with self.lock:
            # noinspection PyBroadException
            try:
                self.__ssh.close()
                self.__sftp = None
            except Exception:
                self.logger.exception("Could not close ssh connection")
                if self.__sftp is not None:
                    # noinspection PyBroadException
                    try:
                        self.__sftp.close()
                    except Exception:
                        self.logger.exception("Could not close sftp connection")

    def __del__(self) -> None:
        """Destructor helper: close channel and threads BEFORE closing others.

        Due to threading in paramiko, default destructor could generate asserts on close,
        so we calling channel close before closing main ssh object.
        """
        try:
            self.__ssh.close()
        except BaseException as e:  # pragma: no cover
            self.logger.debug(f"Exception in {self!s} destructor call: {e}")
        self.__sftp = None

    def __exit__(self, exc_type: typing.Any, exc_val: typing.Any, exc_tb: typing.Any) -> None:
        """Exit context manager.

        .. versionchanged:: 1.0.0 disconnect enforced on close
        .. versionchanged:: 1.1.0 release lock on exit
        .. versionchanged:: 1.2.1 disconnect enforced on close only not in keepalive mode
        """
        if not self.__keepalive_period:
            self.close()
        super(SSHClientBase, self).__exit__(exc_type, exc_val, exc_tb)

    @property
    def sudo_mode(self) -> bool:
        """Persistent sudo mode for connection object.

        :rtype: bool
        """
        return self.__sudo_mode

    @sudo_mode.setter
    def sudo_mode(self, mode: bool) -> None:
        """Persistent sudo mode change for connection object.

        :param mode: sudo status: enabled | disabled
        :type mode: bool
        """
        self.__sudo_mode = bool(mode)

    @property
    def keepalive_period(self) -> int:
        """Keepalive period for connection object.

        :rtype: int
        If 0 - close connection on exit from context manager.
        """
        return self.__keepalive_period

    @keepalive_period.setter
    def keepalive_period(self, period: typing.Union[int, bool]) -> None:
        """Keepalive period change for connection object.

        :param period: keepalive period change
        :type period: typing.Union[int, bool]
        If 0 - close connection on exit from context manager.
        """
        self.__keepalive_period = int(period)
        transport: paramiko.Transport = self.__ssh.get_transport()
        transport.set_keepalive(int(period))

    @property
    def keepalive_mode(self) -> int:
        """Keepalive period for connection object.

        :rtype: int
        If 0 - close connection on exit from context manager.
        """
        warnings.warn('keepalive_mode was moved to keepalive_period as time based parameter', DeprecationWarning)
        return self.__keepalive_period

    @keepalive_mode.setter
    def keepalive_mode(self, period: typing.Union[int, bool]) -> None:
        """Keepalive period change for connection object.

        :param period: keepalive period change
        :type period: typing.Union[int, bool]
        If 0 - close connection on exit from context manager.
        """
        self.__keepalive_period = int(period)
        transport: paramiko.Transport = self.__ssh.get_transport()
        transport.set_keepalive(int(period))

    def reconnect(self) -> None:
        """Reconnect SSH session."""
        with self.lock:
            self.close()
            self.__connect()

    def sudo(self, enforce: typing.Optional[bool] = None) -> "typing.ContextManager[None]":
        """Call contextmanager for sudo mode change.

        :param enforce: Enforce sudo enabled or disabled. By default: None
        :type enforce: typing.Optional[bool]
        :return: context manager with selected sudo state inside
        :rtype: typing.ContextManager[None]
        """
        return _SudoContext(ssh=self, enforce=enforce)

    def keepalive(self, enforce: typing.Union[int, bool] = 1) -> "typing.ContextManager[None]":
        """Call contextmanager with keepalive period change.

        :param enforce: Enforce keepalive period.
        :type enforce: typing.Union[int, bool]
        :return: context manager with selected keepalive state inside
        :rtype: typing.ContextManager[None]

        .. Note:: Enter and exit ssh context manager is produced as well.
        .. versionadded:: 1.2.1
        """
        return _KeepAliveContext(ssh=self, enforce=int(enforce))

    def _prepare_command(self, cmd: str, chroot_path: typing.Optional[str] = None) -> str:
        """Prepare command: cower chroot and other cases.

        :param cmd: main command
        :param chroot_path: path to make chroot for execution
        :return: final command, includes chroot, if required
        """
        if not self.sudo_mode:
            return super()._prepare_command(cmd=cmd, chroot_path=chroot_path)
        if any((chroot_path, self._chroot_path)):
            target_path: str = shlex.quote(chroot_path if chroot_path else self._chroot_path)  # type: ignore
            quoted_command: str = shlex.quote(cmd)
            return f'chroot {target_path} sudo sh -c {shlex.quote(f"eval {quoted_command}")}'
        return f'sudo -S sh -c "eval {shlex.quote(cmd)}"'

    # noinspection PyMethodOverriding
    def _execute_async(  # pylint: disable=arguments-differ
        self,
        command: str,
        stdin: typing.Union[bytes, str, bytearray, None] = None,
        open_stdout: bool = True,
        open_stderr: bool = True,
        *,
        chroot_path: typing.Optional[str] = None,
        get_pty: bool = False,
        width: int = 80,
        height: int = 24,
        **kwargs: typing.Any,
    ) -> SshExecuteAsyncResult:
        """Execute command in async mode and return channel with IO objects.

        :param command: Command for execution
        :type command: str
        :param stdin: pass STDIN text to the process
        :type stdin: typing.Union[bytes, str, bytearray, None]
        :param open_stdout: open STDOUT stream for read
        :type open_stdout: bool
        :param open_stderr: open STDERR stream for read
        :type open_stderr: bool
        :param chroot_path: chroot path override
        :type chroot_path: typing.Optional[str]
        :param get_pty: Get PTY for connection
        :type get_pty: bool
        :param width: PTY width
        :type width: int
        :param height: PTY height
        :type height: int
        :param kwargs: additional parameters for call.
        :type kwargs: typing.Any
        :return: Tuple with control interface and file-like objects for STDIN/STDERR/STDOUT
        :rtype: typing.NamedTuple(
                    'SshExecuteAsyncResult',
                    [
                        ('interface', paramiko.Channel),
                        ('stdin', paramiko.ChannelFile),
                        ('stderr', typing.Optional[paramiko.ChannelFile]),
                        ('stdout', typing.Optional[paramiko.ChannelFile]),
                        ("started", datetime.datetime),
                    ]
                )

        .. versionchanged:: 1.2.0 open_stdout and open_stderr flags
        .. versionchanged:: 1.2.0 stdin data
        .. versionchanged:: 1.2.0 get_pty moved to `**kwargs`
        .. versionchanged:: 2.1.0 Use typed NamedTuple as result
        .. versionchanged:: 3.2.0 Expose pty options as optional keyword-only arguments
        .. versionchanged:: 4.1.0 support chroot
        """
        chan: paramiko.Channel = self._ssh.get_transport().open_session()

        if get_pty:
            # Open PTY
            chan.get_pty(term="vt100", width=width, height=height, width_pixels=0, height_pixels=0)

        _stdin: paramiko.ChannelFile = chan.makefile("wb")
        stdout: paramiko.ChannelFile = chan.makefile("rb")
        stderr: typing.Optional[paramiko.ChannelFile] = chan.makefile_stderr("rb") if open_stderr else None

        cmd = f"{self._prepare_command(cmd=command, chroot_path=chroot_path)}\n"

        started = datetime.datetime.utcnow()
        if self.sudo_mode:
            chan.exec_command(cmd)  # nosec  # Sanitize on caller side
            if stdout.channel.closed is False:
                # noinspection PyTypeChecker
                self.auth.enter_password(_stdin)
                _stdin.flush()
        else:
            chan.exec_command(cmd)  # nosec  # Sanitize on caller side

        if stdin is not None:
            if not _stdin.channel.closed:
                stdin_str: bytes = self._string_bytes_bytearray_as_bytes(stdin)

                _stdin.write(stdin_str)
                _stdin.flush()
            else:
                self.logger.warning("STDIN Send failed: closed channel")

        if open_stdout:
            res_stdout = stdout
        else:
            stdout.close()
            res_stdout = None

        return SshExecuteAsyncResult(interface=chan, stdin=_stdin, stderr=stderr, stdout=res_stdout, started=started)

    def _exec_command(  # type: ignore
        self,
        command: str,
        async_result: SshExecuteAsyncResult,
        timeout: typing.Union[int, float, None],
        verbose: bool = False,
        log_mask_re: typing.Optional[str] = None,
        *,
        stdin: typing.Union[bytes, str, bytearray, None] = None,
        **kwargs: typing.Any,
    ) -> exec_result.ExecResult:
        """Get exit status from channel with timeout.

        :param command: executed command (for logs)
        :type command: str
        :param async_result: execute_async result
        :type async_result: SshExecuteAsyncResult
        :param timeout: timeout before stop execution with TimeoutError
        :type timeout: typing.Union[int, float, None]
        :param verbose: produce log.info records for STDOUT/STDERR
        :type verbose: bool
        :param log_mask_re: regex lookup rule to mask command for logger.
                            all MATCHED groups will be replaced by '<*masked*>'
        :type log_mask_re: typing.Optional[str]
        :param stdin: pass STDIN text to the process
        :type stdin: typing.Union[bytes, str, bytearray, None]
        :param kwargs: additional parameters for call.
        :type kwargs: typing.Any
        :return: Execution result
        :rtype: ExecResult
        :raises ExecHelperTimeoutError: Timeout exceeded

        .. versionchanged:: 1.2.0 log_mask_re regex rule for masking cmd
        """

        def poll_streams() -> None:
            """Poll FIFO buffers if data available."""
            if async_result.stdout and async_result.interface.recv_ready():
                result.read_stdout(src=async_result.stdout, log=self.logger, verbose=verbose)
            if async_result.stderr and async_result.interface.recv_stderr_ready():
                result.read_stderr(src=async_result.stderr, log=self.logger, verbose=verbose)

        @threaded.threadpooled
        def poll_pipes() -> None:
            """Polling task for FIFO buffers."""
            while not async_result.interface.status_event.is_set():
                time.sleep(0.1)
                if async_result.stdout or async_result.stderr:
                    poll_streams()

            result.read_stdout(src=async_result.stdout, log=self.logger, verbose=verbose)
            result.read_stderr(src=async_result.stderr, log=self.logger, verbose=verbose)
            result.exit_code = async_result.interface.exit_status

        # channel.status_event.wait(timeout)
        cmd_for_log: str = self._mask_command(cmd=command, log_mask_re=log_mask_re)

        # Store command with hidden data
        result = exec_result.ExecResult(cmd=cmd_for_log, stdin=stdin, started=async_result.started)

        # noinspection PyNoneFunctionAssignment,PyTypeChecker
        future: "concurrent.futures.Future[None]" = poll_pipes()

        concurrent.futures.wait([future], timeout)

        # Process closed?
        if async_result.interface.status_event.is_set():
            async_result.interface.close()
            return result

        async_result.interface.close()
        async_result.interface.status_event.set()
        future.cancel()

        concurrent.futures.wait([future], 0.001)
        result.set_timestamp()

        wait_err_msg: str = _log_templates.CMD_WAIT_ERROR.format(result=result, timeout=timeout)
        self.logger.debug(wait_err_msg)
        raise exceptions.ExecHelperTimeoutError(result=result, timeout=timeout)  # type: ignore

    def _get_proxy_channel(self, port: typing.Optional[int], ssh_config: SSHConfig,) -> paramiko.Channel:
        """Get ssh proxy channel.

        :param port: target port
        :type port: typing.Optional[int]
        :param ssh_config: pre-parsed ssh config
        :type ssh_config: SSHConfig
        :return: ssh channel for usage as socket for new connection over it
        :rtype: paramiko.Channel

        .. versionadded:: 6.0.0
        """
        dest_port: int = port if port is not None else ssh_config.port if ssh_config.port is not None else 22

        return self._ssh.get_transport().open_channel(
            kind="direct-tcpip", dest_addr=(ssh_config.hostname, dest_port), src_addr=(self.hostname, 0)
        )

    def proxy_to(
        self,
        host: str,
        port: typing.Optional[int] = None,
        username: typing.Optional[str] = None,
        password: typing.Optional[str] = None,
        *,
        auth: typing.Optional[ssh_auth.SSHAuth] = None,
        verbose: bool = True,
        ssh_config: typing.Union[
            str,
            paramiko.SSHConfig,
            typing.Dict[str, typing.Dict[str, typing.Union[str, int, bool, typing.List[str]]]],
            HostsSSHConfigs,
            None,
        ] = None,
        ssh_auth_map: typing.Optional[typing.Union[typing.Dict[str, ssh_auth.SSHAuth], ssh_auth.SSHAuthMapping]] = None,
        keepalive: typing.Union[int, bool] = 1,
    ) -> "SSHClientBase":
        """Start new SSH connection using current as proxy.

        :param host: remote hostname
        :type host: str
        :param port: remote ssh port
        :type port: typing.Optional[int]
        :param username: remote username.
        :type username: typing.Optional[str]
        :param password: remote password
        :type password: typing.Optional[str]
        :param auth: credentials for connection
        :type auth: typing.Optional[ssh_auth.SSHAuth]
        :param verbose: show additional error/warning messages
        :type verbose: bool
        :param ssh_config: SSH configuration for connection. Maybe config path, parsed as dict and paramiko parsed.
        :type ssh_config:
            typing.Union[
                str,
                paramiko.SSHConfig,
                typing.Dict[str, typing.Dict[str, typing.Union[str, int, bool, typing.List[str]]]],
                HostsSSHConfigs,
                None
            ]
        :param ssh_auth_map: SSH authentication information mapped to host names. Useful for complex SSH Proxy cases.
        :type ssh_auth_map: typing.Optional[typing.Union[typing.Dict[str, ssh_auth.SSHAuth], ssh_auth.SSHAuthMapping]]
        :param keepalive: keepalive period
        :type keepalive: typing.Union[int, bool]
        :return: new ssh client instance using current as a proxy
        :rtype: SSHClientBase

        .. note:: auth has priority over username/password

        .. versionadded:: 6.0.0
        """
        if isinstance(ssh_config, HostsSSHConfigs):
            parsed_ssh_config: HostsSSHConfigs = ssh_config
        else:
            parsed_ssh_config = _ssh_helpers.parse_ssh_config(ssh_config, host)

        hostname = parsed_ssh_config[host].hostname

        sock: paramiko.Channel = self._get_proxy_channel(port=port, ssh_config=parsed_ssh_config[hostname])
        cls: typing.Type[SSHClientBase] = self.__class__
        return cls(
            host=host,
            port=port,
            username=username,
            password=password,
            auth=auth,
            verbose=verbose,
            ssh_config=ssh_config,
            sock=sock,
            ssh_auth_map=ssh_auth_map if ssh_auth_map is not None else self.__auth_mapping,
            keepalive=int(keepalive),
        )

    def execute_through_host(
        self,
        hostname: str,
        command: str,
        *,
        auth: typing.Optional[ssh_auth.SSHAuth] = None,
        port: typing.Optional[int] = None,
        target_port: typing.Optional[int] = None,
        verbose: bool = False,
        timeout: typing.Union[int, float, None] = constants.DEFAULT_TIMEOUT,
        open_stdout: bool = True,
        open_stderr: bool = True,
        stdin: typing.Union[bytes, str, bytearray, None] = None,
        log_mask_re: typing.Optional[str] = None,
        get_pty: bool = False,
        width: int = 80,
        height: int = 24,
    ) -> exec_result.ExecResult:
        """Execute command on remote host through currently connected host.

        :param hostname: target hostname
        :type hostname: str
        :param command: Command for execution
        :type command: str
        :param auth: credentials for target machine
        :type auth: typing.Optional[ssh_auth.SSHAuth]
        :param port: target port
        :type port: typing.Optional[int]
        :param target_port: target port (deprecated in favor of port)
        :type target_port: typing.Optional[int]
        :param verbose: Produce log.info records for command call and output
        :type verbose: bool
        :param timeout: Timeout for command execution.
        :type timeout: typing.Union[int, float, None]
        :param open_stdout: open STDOUT stream for read
        :type open_stdout: bool
        :param open_stderr: open STDERR stream for read
        :type open_stderr: bool
        :param stdin: pass STDIN text to the process
        :type stdin: typing.Union[bytes, str, bytearray, None]
        :param log_mask_re: regex lookup rule to mask command for logger.
                            all MATCHED groups will be replaced by '<*masked*>'
        :type log_mask_re: typing.Optional[str]
        :param get_pty: open PTY on target machine
        :type get_pty: bool
        :param width: PTY width
        :type width: int
        :param height: PTY height
        :type height: int
        :return: Execution result
        :rtype: ExecResult
        :raises ExecHelperTimeoutError: Timeout exceeded

        .. versionchanged:: 1.2.0 default timeout 1 hour
        .. versionchanged:: 1.2.0 log_mask_re regex rule for masking cmd
        .. versionchanged:: 3.2.0 Expose pty options as optional keyword-only arguments
        .. versionchanged:: 4.0.0 Expose stdin and log_mask_re as optional keyword-only arguments
        .. versionchanged:: 6.0.0 Move channel open to separate method and make proper ssh-proxy usage
        .. versionchanged:: 6.0.0 only hostname and command are positional argument, target_port changed to port.
        """
        conn: SSHClientBase
        if auth is None:
            auth = self.auth

        if target_port is not None:  # pragma: no cover
            warnings.warn(
                f'"target_port" argument was renamed to "port". '
                f"Old version will be droppped in the next major release.",
                DeprecationWarning,
            )
            port = target_port

        with self.proxy_to(  # type: ignore
            host=hostname, port=port, auth=auth, verbose=verbose, ssh_config=self.ssh_config, keepalive=False
        ) as conn:
            return conn(
                command,
                timeout=timeout,
                open_stdout=open_stdout,
                open_stderr=open_stderr,
                stdin=stdin,
                log_mask_re=log_mask_re,
                get_pty=get_pty,
                width=width,
                height=height,
            )

    @classmethod
    def execute_together(
        cls,
        remotes: typing.Iterable["SSHClientBase"],
        command: str,
        timeout: typing.Union[int, float, None] = constants.DEFAULT_TIMEOUT,
        expected: typing.Iterable[typing.Union[int, proc_enums.ExitCodes]] = (proc_enums.EXPECTED,),
        raise_on_err: bool = True,
        *,
        stdin: typing.Union[bytes, str, bytearray, None] = None,
        verbose: bool = False,
        log_mask_re: typing.Optional[str] = None,
        exception_class: "typing.Type[exceptions.ParallelCallProcessError]" = exceptions.ParallelCallProcessError,
        **kwargs: typing.Any,
    ) -> typing.Dict[typing.Tuple[str, int], exec_result.ExecResult]:
        """Execute command on multiple remotes in async mode.

        :param remotes: Connections to execute on
        :type remotes: typing.Iterable[SSHClientBase]
        :param command: Command for execution
        :type command: str
        :param timeout: Timeout for command execution.
        :type timeout: typing.Union[int, float, None]
        :param expected: expected return codes (0 by default)
        :type expected: typing.Iterable[typing.Union[int, proc_enums.ExitCodes]]
        :param raise_on_err: Raise exception on unexpected return code
        :type raise_on_err: bool
        :param stdin: pass STDIN text to the process
        :type stdin: typing.Union[bytes, str, bytearray, None]
        :param verbose: produce verbose log record on command call
        :type verbose: bool
        :param log_mask_re: regex lookup rule to mask command for logger.
                            all MATCHED groups will be replaced by '<*masked*>'
        :type log_mask_re: typing.Optional[str]
        :param exception_class: Exception to raise on error. Mandatory subclass of exceptions.ParallelCallProcessError
        :type exception_class: typing.Type[exceptions.ParallelCallProcessError]
        :param kwargs: additional parameters for execute_async call.
        :type kwargs: typing.Any
        :return: dictionary {(hostname, port): result}
        :rtype: typing.Dict[typing.Tuple[str, int], exec_result.ExecResult]
        :raises ParallelCallProcessError: Unexpected any code at lest on one target
        :raises ParallelCallExceptions: At lest one exception raised during execution (including timeout)

        .. versionchanged:: 1.2.0 default timeout 1 hour
        .. versionchanged:: 1.2.0 log_mask_re regex rule for masking cmd
        .. versionchanged:: 3.2.0 Exception class can be substituted
        .. versionchanged:: 3.4.0 Expected is not optional, defaults os dependent
        .. versionchanged:: 4.0.0 Expose stdin and log_mask_re as optional keyword-only arguments
        """

        @threaded.threadpooled
        def get_result(remote: "SSHClientBase") -> exec_result.ExecResult:
            """Get result from remote call.

            :param remote: SSH connection instance
            :return: execution result
            """
            # pylint: disable=protected-access
            cmd_for_log: str = remote._mask_command(cmd=command, log_mask_re=log_mask_re)

            remote.logger.log(
                level=logging.INFO if verbose else logging.DEBUG, msg=f"Executing command:\n{cmd_for_log!r}\n"
            )
            async_result: SshExecuteAsyncResult = remote._execute_async(
                command, stdin=stdin, log_mask_re=log_mask_re, **kwargs
            )
            # pylint: enable=protected-access

            async_result.interface.status_event.wait(timeout)
            exit_code = async_result.interface.recv_exit_status()

            res = exec_result.ExecResult(cmd=cmd_for_log, stdin=stdin, started=async_result.started)
            res.read_stdout(src=async_result.stdout)
            res.read_stderr(src=async_result.stderr)
            res.exit_code = exit_code

            async_result.interface.close()
            return res

        prep_expected: typing.Tuple[typing.Union[int, proc_enums.ExitCodes], ...] = proc_enums.exit_codes_to_enums(
            expected
        )

        futures: typing.Dict["SSHClientBase", "concurrent.futures.Future[exec_result.ExecResult]"] = {
            remote: get_result(remote) for remote in set(remotes)
        }  # Use distinct remotes
        results: typing.Dict[typing.Tuple[str, int], exec_result.ExecResult] = {}
        errors: typing.Dict[typing.Tuple[str, int], exec_result.ExecResult] = {}
        raised_exceptions: typing.Dict[typing.Tuple[str, int], Exception] = {}

        _, not_done = concurrent.futures.wait(list(futures.values()), timeout=timeout)

        for fut in not_done:  # pragma: no cover
            fut.cancel()

        for (remote, future) in futures.items():
            try:
                result = future.result()
                results[(remote.hostname, remote.port)] = result
                if result.exit_code not in prep_expected:
                    errors[(remote.hostname, remote.port)] = result
            except Exception as e:
                raised_exceptions[(remote.hostname, remote.port)] = e

        if raised_exceptions:  # always raise
            raise exceptions.ParallelCallExceptions(command, raised_exceptions, errors, results, expected=prep_expected)
        if errors and raise_on_err:
            raise exception_class(command, errors, results, expected=prep_expected)
        return results

    def open(self, path: str, mode: str = "r") -> paramiko.SFTPFile:
        """Open file on remote using SFTP session.

        :param path: filesystem object path
        :type path: str
        :param mode: open file mode ('t' is not supported)
        :type mode: str
        :return: file.open() stream
        :rtype: paramiko.SFTPFile
        """
        return self._sftp.open(path, mode)  # pragma: no cover

    def exists(self, path: str) -> bool:
        """Check for file existence using SFTP session.

        :param path: filesystem object path
        :type path: str
        :return: path is valid (object exists)
        :rtype: bool
        """
        try:
            self._sftp.lstat(path)
            return True
        except IOError:
            return False

    def stat(self, path: str) -> paramiko.sftp_attr.SFTPAttributes:
        """Get stat info for path with following symlinks.

        :param path: filesystem object path
        :type path: str
        :return: stat like information for remote path
        :rtype: paramiko.sftp_attr.SFTPAttributes
        """
        return self._sftp.stat(path)  # pragma: no cover

    def utime(self, path: str, times: typing.Optional[typing.Tuple[int, int]] = None) -> None:
        """Set atime, mtime.

        :param path: filesystem object path
        :type path: str
        :param times: (atime, mtime)
        :type times: typing.Optional[typing.Tuple[int, int]]

        .. versionadded:: 1.0.0
        """
        self._sftp.utime(path, times)  # pragma: no cover

    def isfile(self, path: str) -> bool:
        """Check, that path is file using SFTP session.

        :param path: remote path to validate
        :type path: str
        :return: path is file
        :rtype: bool
        """
        try:
            attrs: paramiko.sftp_attr.SFTPAttributes = self._sftp.lstat(path)
            return stat.S_ISREG(attrs.st_mode)
        except IOError:
            return False

    def isdir(self, path: str) -> bool:
        """Check, that path is directory using SFTP session.

        :param path: remote path to validate
        :type path: str
        :return: path is directory
        :rtype: bool
        """
        try:
            attrs: paramiko.sftp_attr.SFTPAttributes = self._sftp.lstat(path)
            return stat.S_ISDIR(attrs.st_mode)
        except IOError:
            return False

    def islink(self, path: str) -> bool:
        """Check, that path is symlink using SFTP session.

        :param path: remote path to validate
        :type path: str
        :return: path is symlink
        :rtype: bool
        """
        try:
            attrs: paramiko.sftp_attr.SFTPAttributes = self._sftp.lstat(path)
            return stat.S_ISLNK(attrs.st_mode)
        except IOError:
            return False

    def symlink(self, source: str, dest: str) -> None:
        """Produce symbolic link like `os.symlink`.

        :param source: source path
        :type source: str
        :param dest: source path
        :type dest: str
        """
        self._sftp.symlink(source, dest)  # pragma: no cover

    def chmod(self, path: str, mode: int) -> None:
        """Change the mode (permissions) of a file like `os.chmod`.

        :param path: filesystem object path
        :type path: str
        :param mode: new permissions
        :type mode: int
        """
        self._sftp.chmod(path, mode)  # pragma: no cover
