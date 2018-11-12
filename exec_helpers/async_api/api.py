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

"""Async API.

.. versionadded:: 3.0.0
"""

__all__ = ("ExecHelper",)

import abc
import asyncio
import logging
import typing

from exec_helpers import api
from exec_helpers import constants
from exec_helpers import exec_result
from exec_helpers import exceptions
from exec_helpers import proc_enums


class ExecHelper(api.ExecHelper, metaclass=abc.ABCMeta):
    """Subprocess helper with timeouts and lock-free FIFO."""

    __slots__ = ("__alock",)

    def __init__(self, logger: logging.Logger, log_mask_re: typing.Optional[str] = None) -> None:
        """Subprocess helper with timeouts and lock-free FIFO.

        :param logger: logger instance to use
        :type logger: logging.Logger
        :param log_mask_re: regex lookup rule to mask command for logger.
                            all MATCHED groups will be replaced by '<*masked*>'
        :type log_mask_re: typing.Optional[str]
        """
        super(ExecHelper, self).__init__(logger=logger, log_mask_re=log_mask_re)
        self.__alock = None  # type: typing.Optional[asyncio.Lock]

    async def __aenter__(self) -> "ExecHelper":
        """Async context manager."""
        if self.__alock is None:
            self.__alock = asyncio.Lock()
        await self.__alock.acquire()
        return self

    async def __aexit__(self, exc_type: typing.Any, exc_val: typing.Any, exc_tb: typing.Any) -> None:
        """Async context manager."""
        self.__alock.release()  # type: ignore

    @abc.abstractmethod
    async def _exec_command(  # type: ignore
        self,
        command: str,
        async_result: api.ExecuteAsyncResult,
        timeout: typing.Union[int, float, None],
        verbose: bool = False,
        log_mask_re: typing.Optional[str] = None,
        **kwargs: typing.Any
    ) -> exec_result.ExecResult:
        """Get exit status from channel with timeout.

        :param command: Command for execution
        :type command: str
        :param interface: Control interface
        :type interface: typing.Any
        :param stdout: STDOUT pipe or file-like object
        :type stdout: typing.Optional[asyncio.StreamReader]
        :param stderr: STDERR pipe or file-like object
        :type stderr: typing.Optional[asyncio.StreamReader]
        :param timeout: Timeout for command execution
        :type timeout: typing.Union[int, float, None]
        :param verbose: produce verbose log record on command call
        :type verbose: bool
        :param log_mask_re: regex lookup rule to mask command for logger.
                            all MATCHED groups will be replaced by '<*masked*>'
        :type log_mask_re: typing.Optional[str]
        :param kwargs: additional parameters for call.
        :type kwargs: typing.Any
        :return: Execution result
        :rtype: ExecResult
        :raises OSError: exception during process kill (and not regarding to already closed process)
        :raises ExecHelperTimeoutError: Timeout exceeded
        """
        raise NotImplementedError  # pragma: no cover

    @abc.abstractmethod
    async def execute_async(  # type: ignore
        self,
        command: str,
        stdin: typing.Union[str, bytes, bytearray, None] = None,
        open_stdout: bool = True,
        open_stderr: bool = True,
        verbose: bool = False,
        log_mask_re: typing.Optional[str] = None,
        **kwargs: typing.Any
    ) -> api.ExecuteAsyncResult:
        """Execute command in async mode and return Popen with IO objects.

        :param command: Command for execution
        :type command: str
        :param stdin: pass STDIN text to the process
        :type stdin: typing.Union[str, bytes, bytearray, None]
        :param open_stdout: open STDOUT stream for read
        :type open_stdout: bool
        :param open_stderr: open STDERR stream for read
        :type open_stderr: bool
        :param verbose: produce verbose log record on command call
        :type verbose: bool
        :param log_mask_re: regex lookup rule to mask command for logger.
                            all MATCHED groups will be replaced by '<*masked*>'
        :type log_mask_re: typing.Optional[str]
        :param kwargs: additional parameters for call.
        :type kwargs: typing.Any
        :return: Tuple with control interface and file-like objects for STDIN/STDERR/STDOUT
        :rtype: typing.NamedTuple(
                    'ExecuteAsyncResult',
                    [
                        ('interface', typing.Any),
                        ('stdin', typing.Optional[typing.Any]),
                        ('stderr', typing.Optional[typing.Any]),
                        ('stdout', typing.Optional[typing.Any]),
                    ]
                )
        :raises OSError: impossible to process STDIN
        """
        raise NotImplementedError  # pragma: no cover

    async def execute(  # type: ignore
        self,
        command: str,
        verbose: bool = False,
        timeout: typing.Union[int, float, None] = constants.DEFAULT_TIMEOUT,
        **kwargs: typing.Any
    ) -> exec_result.ExecResult:
        """Execute command and wait for return code.

        :param command: Command for execution
        :type command: str
        :param verbose: Produce log.info records for command call and output
        :type verbose: bool
        :param timeout: Timeout for command execution.
        :type timeout: typing.Union[int, float, None]
        :param kwargs: additional parameters for call.
        :type kwargs: typing.Any
        :return: Execution result
        :rtype: ExecResult
        :raises ExecHelperTimeoutError: Timeout exceeded
        """
        async_result = await self.execute_async(command, verbose=verbose, **kwargs)

        result = await self._exec_command(
            command=command, async_result=async_result, timeout=timeout, verbose=verbose, **kwargs
        )
        message = "Command {result.cmd!r} exit code: {result.exit_code!s}".format(result=result)
        self.logger.log(level=logging.INFO if verbose else logging.DEBUG, msg=message)  # type: ignore
        return result

    async def check_call(  # type: ignore
        self,
        command: str,
        verbose: bool = False,
        timeout: typing.Union[int, float, None] = constants.DEFAULT_TIMEOUT,
        error_info: typing.Optional[str] = None,
        expected: typing.Optional[typing.Iterable[typing.Union[int, proc_enums.ExitCodes]]] = None,
        raise_on_err: bool = True,
        **kwargs: typing.Any
    ) -> exec_result.ExecResult:
        """Execute command and check for return code.

        :param command: Command for execution
        :type command: str
        :param verbose: Produce log.info records for command call and output
        :type verbose: bool
        :param timeout: Timeout for command execution.
        :type timeout: typing.Union[int, float, None]
        :param error_info: Text for error details, if fail happens
        :type error_info: typing.Optional[str]
        :param expected: expected return codes (0 by default)
        :type expected: typing.Optional[typing.Iterable[typing.Union[int, proc_enums.ExitCodes]]]
        :param raise_on_err: Raise exception on unexpected return code
        :type raise_on_err: bool
        :param kwargs: additional parameters for call.
        :type kwargs: typing.Any
        :return: Execution result
        :rtype: ExecResult
        :raises ExecHelperTimeoutError: Timeout exceeded
        :raises CalledProcessError: Unexpected exit code
        """
        expected_codes = proc_enums.exit_codes_to_enums(expected)
        ret = await self.execute(command, verbose, timeout, **kwargs)
        if ret.exit_code not in expected_codes:
            message = (
                "{append}Command {result.cmd!r} returned exit code "
                "{result.exit_code!s} while expected {expected!s}".format(
                    append=error_info + "\n" if error_info else "", result=ret, expected=expected_codes
                )
            )
            self.logger.error(msg=message)
            if raise_on_err:
                raise exceptions.CalledProcessError(result=ret, expected=expected_codes)
        return ret

    async def check_stderr(  # type: ignore
        self,
        command: str,
        verbose: bool = False,
        timeout: typing.Union[int, float, None] = constants.DEFAULT_TIMEOUT,
        error_info: typing.Optional[str] = None,
        raise_on_err: bool = True,
        **kwargs: typing.Any
    ) -> exec_result.ExecResult:
        """Execute command expecting return code 0 and empty STDERR.

        :param command: Command for execution
        :type command: str
        :param verbose: Produce log.info records for command call and output
        :type verbose: bool
        :param timeout: Timeout for command execution.
        :type timeout: typing.Union[int, float, None]
        :param error_info: Text for error details, if fail happens
        :type error_info: typing.Optional[str]
        :param raise_on_err: Raise exception on unexpected return code
        :type raise_on_err: bool
        :param kwargs: additional parameters for call.
        :type kwargs: typing.Any
        :return: Execution result
        :rtype: ExecResult
        :raises ExecHelperTimeoutError: Timeout exceeded
        :raises CalledProcessError: Unexpected exit code or stderr presents
        """
        ret = await self.check_call(
            command, verbose, timeout=timeout, error_info=error_info, raise_on_err=raise_on_err, **kwargs
        )
        if ret.stderr:
            message = (
                "{append}Command {result.cmd!r} output contains STDERR while not expected\n"
                "\texit code: {result.exit_code!s}".format(append=error_info + "\n" if error_info else "", result=ret)
            )
            self.logger.error(msg=message)
            if raise_on_err:
                raise exceptions.CalledProcessError(result=ret, expected=kwargs.get("expected"))
        return ret