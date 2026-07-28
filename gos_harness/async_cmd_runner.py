"""
This file holds functions that deal with asyncio event loop.
Most notably, to avoid any blocking, we wrap all subprocess calls in an async method
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional, Sequence, TypeVar
from dataclasses import dataclass
from concurrent.futures.thread import ThreadPoolExecutor

import math
import subprocess
import time
import asyncio
import os
import signal
import errno
from contextlib import suppress

from gos_harness import gos_logging as logging
from gos_harness.utils.module_loader import LazyLoader

T = TypeVar("T")

tqdm = LazyLoader("tqdm", globals(), "tqdm")


@dataclass
class BatchRunResult():
    """Encapsulation of the result of the batch run of jobs

    :ivar successes: list of successfully finished jobs
    :ivar failures: list of failed jobs
    :ivar aborted: true if the batch run was aborted early
    """
    all_results: list[Any]
    successes: list[Any]
    failures: list[Any]
    aborted: bool
    timeout: bool

    def assert_run_was_successful(self, error_msg: str = "", raise_on_assertion_failure: bool = False) -> None:
        """Helper function that asserts that there were no failures; if there were, it will end with Runtime error and print the exceptions

        :param error_msg: error message printed in the exceptions
        :param raise_on_assertion_failure: if there should be a exception raised
        :raises RuntimeError: if there were failures and raise_on_assertion_failure was set to true
        """
        number_of_failures = len(self.failures)
        if number_of_failures > 0:
            if raise_on_assertion_failure:
                logging.log_exceptions(self.failures)
                raise RuntimeError(f"{error_msg}{number_of_failures} batch jobs failed")
            else:
                logging.log(f"[Warning] {error_msg}{number_of_failures} batch jobs failed")
                logging.log_exceptions(self.failures)
        if self.timeout:
            raise TimeoutError(f"{error_msg} scenario timeout hit.")


_loop: Optional[asyncio.AbstractEventLoop] = None

def get_loop() -> asyncio.AbstractEventLoop:
    """Lazy initialization of the async loop.

    We need the loop to be initialized lazily so the objects can be pickled and run with gos-rpc later.
    Otherwise, the loop would be pickled together with the object and after unpickling and running
    the stored procedures the async functions would results into RunTime error.
    """
    global _loop
    if _loop is None:
        _loop = asyncio.get_event_loop()
    return _loop

# we want to make sure to never reach the max concurrency allowed by the thread pool since the harness deals with
# all time sensitive operations already.
# The tasks sent to the pool are subprocess calls anyway which must be long-running and require little CPU & IO.
THREAD_POOL = ThreadPoolExecutor(max_workers=100000)


def wait_for(task: Awaitable[T]) -> T:
    """
    Wait for completion of a single coroutine
    """
    tasks = [task]
    task_results = get_loop().run_until_complete(asyncio.gather(*tasks))
    return task_results[0]


def append_list_to_env_var(environment: dict[str, str], variable_name: str, new_entries: Sequence[Optional[str]]) -> dict[str, str]:
    """
    Modifies `variable_name` from the `environment` and adds a comma-separated list of `new_entries` to its value.

    :param dict environment: the source environment to modify
    :param str variable_name: the variable to modify or create in the environment
    :param list[str] new_entries: the list entries to append to the given variable
    :return: the updated environment
    """
    if variable_name not in environment:
        environment[variable_name] = ""
    no_proxy_entries = environment[variable_name].split(',') + [entry for entry in new_entries if entry is not None]
    environment[variable_name] = ','.join(no_proxy_entries)
    return environment


async def terminate_proc_group(proc: asyncio.subprocess.Process, grace_period: float = 5) -> None:
    """Terminates the process (first gracefully, then roughly)

    :param proc: process we are terminating
    :param grace: grace period before we send nukes
    """
    # First we send SIGTERM, trying to be nice
    with suppress(Exception):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    # We wait for short time for process to terminate gracefully
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_period)
    except asyncio.TimeoutError:
        # Sending the nukes
        with suppress(Exception):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        await proc.wait()


async def run(
    cmd: list[str],
    caller_name: Optional[str] = None,
    timeout: int = 360,
    quiet: bool = False,
    env: Optional[dict[str, str]] = None,
) -> tuple[subprocess.CompletedProcess[bytes], float, float]:
    """
    Execute an external command asynchronously using asyncio subprocesses.
    - Non-blocking for the event loop
    - Cancellation- and timeout-aware: terminates the whole process group

    :param list cmd: command to execute (list of strings)
    :param str caller_name: identifier for logging context
    :param int timeout: seconds before the command is forcefully terminated (<=0 means no timeout)
    :param bool quiet: if True, suppress logs
    :param dict env: environment for the command
    :return: (CompletedProcess-like result, start_ts, end_ts)
    """
    if not quiet:
        logging.log(f"{caller_name + ':: ' if caller_name else ''}{' '.join(cmd)}")

    start_ts = time.time()

    # Spawn in new process group so we can kill the whole tree on cancel/timeout
    #   - start_new_session ensures the child gets its own process group so terminate_proc_group() does not kill us
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    # Catching specific error: when the command is not found
    #  - we construct process as a return value
    except FileNotFoundError as e:
        end_ts = time.time()
        completed = subprocess.CompletedProcess(args=cmd, returncode=127, stdout=b"", stderr=str(e).encode("utf-8"))
        if not quiet:
            logging.log(f"ERROR: command not found: {cmd[0]}")
        return completed, start_ts, end_ts
    except OSError as e:
        if e.errno in {errno.EMFILE, errno.ENFILE}:
            end_ts = time.time()
            completed = subprocess.CompletedProcess(args=cmd, returncode=127, stdout=b"", stderr=str(e).encode("utf-8"))
            if not quiet:
                logging.log(f"ERROR: Failed to spawn subprocess for command '{cmd[0]}' because the file descriptor limit was reached (errno {e.errno}: {e.strerror}). Increase the open file limit for the load tester process (e.g. `ulimit -n 65536`) or reduce concurrent subprocesses.")
            return completed, start_ts, end_ts
        raise

    try:
        if timeout and timeout > 0:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        else:
            stdout, stderr = await proc.communicate()
        end_ts = time.time()
        assert proc.returncode is not None
        completed = subprocess.CompletedProcess(args=cmd, returncode=proc.returncode, stdout=stdout, stderr=stderr)
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        # We terminate the command properly
        await terminate_proc_group(proc)
        # Map timeouts to subprocess.TimeoutExpired for backward compatibility; re-raise cancellation
        if isinstance(e, asyncio.TimeoutError):
            raise subprocess.TimeoutExpired(cmd=" ".join(cmd), timeout=timeout)
        raise

    if completed.returncode != 0 and not quiet:
        logging.log(f"ERROR: command exit code: {completed.returncode}")
        logging.raw_log("STDERR:\n===")
        logging.raw_log(completed.stderr.decode('utf-8') if completed.stderr is not None else "")
        logging.raw_log("STDOUT:\n===")
    if not quiet:
        output = completed.stdout.decode('utf-8').strip()
        logging.raw_log(output if output else "<no output>")
        logging.log(f"'{cmd[0]}' command completed in {end_ts - start_ts:.3f} sec")

    return completed, start_ts, end_ts

async def batch_run(
    batch_items: list[Any],
    batch_fun: Callable[[Any], Any],
    batch_step: int = 1,
    abort_early: bool = False,
    timeout_handler: Optional[Callable[[], None]] = None,
    progress_label: Optional[str] = None,
) -> BatchRunResult:
    """Runs batch of batch_fun functions on list of batch_items in batch_step

    :param batch_run: list of items that are used as an argument from the batch_fun function
    :param batch_fun: function that will be performed on the items from the batch_items
    :param batch_step: step of the batch run (i.e. how many jobs will be run in single step)
    :param abort_early: if set to true then the batch run will fail once there is a failure in a batch run
    :param progress_label: when set, show a tqdm progress bar instead of logging each batch
    :return: BatchRunResult object, which includes list of successes and list of failures and if the run was aborted early
    """
    batch, batch_items_size = 0, len(batch_items)
    batch_no = math.ceil(batch_items_size / batch_step)
    results, aborted_early, timeout = [], False, False
    progress = None
    if progress_label and batch_items_size > 0:
        try:
            progress = tqdm.tqdm(
                total=batch_items_size,
                desc=progress_label,
                bar_format="{bar} {n_fmt}/{total_fmt} {elapsed} | {desc}",
            )
        except ModuleNotFoundError:
            pass
    try:
        for i in range(0, batch_items_size, batch_step):
            batch += 1
            if progress_label is None:
                logging.log(f" - running batch jobs {batch}/{batch_no}")

            batch_coroutines = [batch_fun(item) for item in batch_items[i:i + batch_step]]
            batch_results = await asyncio.gather(*batch_coroutines, return_exceptions=True)
            results += batch_results
            if progress is not None:
                progress.update(len(batch_results))

            # Retiring early with partial results only
            if abort_early and any(isinstance(result, BaseException) for result in batch_results):
                aborted_early = True
                break

            if timeout_handler:
                try:
                    timeout_handler()
                except TimeoutError:
                    timeout = True
                    break
    finally:
        if progress is not None:
            progress.close()

    # Split the results to successes and failures/exceptions
    successes, failures = [], []
    for result in results:
        if isinstance(result, BaseException):
            failures.append(result)
        else:
            successes.append(result)
    return BatchRunResult(results, successes, failures, aborted_early, timeout)
