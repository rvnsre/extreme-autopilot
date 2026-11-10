"""Create Windows children suspended, contain them, and only then allow their first instruction."""

import os
import signal
import subprocess
from contextlib import contextmanager


class WindowsProcess:
    def __init__(self, argv, cwd, env, before_start):
        import msvcrt

        import win32api
        import win32con
        import win32job
        import win32process

        self.handle = self.job = self.stdout = None
        self.returncode = None
        thread = None
        read_fd = write_fd = null_fd = None
        try:
            self.job = win32job.CreateJobObject(None, "")
            limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
            read_fd, write_fd = os.pipe()
            null_fd = os.open(os.devnull, os.O_RDONLY)
            os.set_inheritable(write_fd, True)
            os.set_inheritable(null_fd, True)
            startup = win32process.STARTUPINFO()
            startup.dwFlags = win32con.STARTF_USESTDHANDLES
            startup.hStdInput = msvcrt.get_osfhandle(null_fd)
            startup.hStdOutput = startup.hStdError = msvcrt.get_osfhandle(write_fd)
            flags = (
                win32process.CREATE_SUSPENDED
                | win32process.CREATE_NO_WINDOW
                | win32process.CREATE_UNICODE_ENVIRONMENT
            )
            self.handle, thread, self.pid, _ = win32process.CreateProcess(
                argv[0], subprocess.list2cmdline(argv), None, None, True, flags, env, str(cwd), startup
            )
            win32job.AssignProcessToJobObject(self.job, self.handle)
            before_start()
            win32process.ResumeThread(thread)
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            read_fd = None
        except BaseException:
            if self.handle:
                win32api.TerminateProcess(self.handle, 1)
            self.close()
            raise
        finally:
            if thread:
                thread.Close()
            for fd in (read_fd, write_fd, null_fd):
                if fd is not None:
                    os.close(fd)

    def poll(self):
        import win32event
        import win32process

        if self.returncode is None and win32event.WaitForSingleObject(self.handle, 0) == 0:
            self.returncode = win32process.GetExitCodeProcess(self.handle)
        return self.returncode

    def wait(self, timeout):
        import win32event

        if win32event.WaitForSingleObject(self.handle, int(timeout * 1000)) != 0:
            raise subprocess.TimeoutExpired("worker child", timeout)
        return self.poll()

    def kill_tree(self):
        import win32job

        if self.job:
            try:
                win32job.TerminateJobObject(self.job, 1)
            finally:
                self.job.Close()
                self.job = None

    def close(self):
        if self.job:
            self.job.Close()
            self.job = None
        if self.handle:
            self.handle.Close()
            self.handle = None
        if self.stdout:
            self.stdout.close()
            self.stdout = None


def kill_tree(process):
    if isinstance(process, WindowsProcess):
        process.kill_tree()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@contextmanager
def managed_process(argv, cwd, env, before_start):
    if os.name == "nt":
        process = WindowsProcess(argv, cwd, env, before_start)
    else:
        before_start()
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        yield process
    finally:
        try:
            kill_tree(process)
            process.wait(timeout=15)
        finally:
            if isinstance(process, WindowsProcess):
                process.close()
            else:
                process.stdout.close()
