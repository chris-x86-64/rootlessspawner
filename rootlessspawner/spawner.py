""" A Spawner for JupyterHub to allow users to execute Jupyter
without involving root, sudo, or local system accounts. """

# Copyright (c) Christopher H Smith <chris@binc.jp>
# Distributed under the terms of the Modified BSD License.

import os
import errno
import signal
import pipes
import shutil
from tornado import gen
from subprocess import Popen
from jupyterhub.spawner import Spawner
from jupyterhub.utils import random_port
from traitlets import (Unicode, Integer, Instance)

class RootlessSpawner(Spawner):

    INTERRUPT_TIMEOUT = Integer(10,
        help="Seconds to wait for process to halt after SIGINT before proceeding to SIGTERM"
    ).tag(config=True)
    TERM_TIMEOUT = Integer(5,
        help="Seconds to wait for process to halt after SIGTERM before proceeding to SIGKILL"
    ).tag(config=True)
    KILL_TIMEOUT = Integer(5,
        help="Seconds to wait for process to halt after SIGKILL before giving up"
    ).tag(config=True)

    shared_dir = Unicode(
        config=True,
        help="The path to a directory shared by all users"
    )

    proc = Instance(Popen, allow_none=True)
    pid = Integer(0)

    def _notebook_dir_validate(self, value, trait):
        # Strip any trailing slashes
        # *except* if it's root
        _, path = os.path.splitdrive(value)
        if path == os.sep:
            return value

        value = value.rstrip(os.sep)

        if not os.path.isabs(value):
            # If we receive a non-absolute path, make it absolute.
            value = os.path.abspath(value)
        if not os.path.isdir(value):
            os.mkdir(value, mode=0o755)

        if self.shared_dir and not os.path.islink(value + '/Shared'):
            # Create a symlink to the shared directory
            os.symlink(self.shared_dir, value + '/Shared')

        return value

    def load_state(self, state):
        """load pid from state"""
        super(RootlessSpawner, self).load_state(state)
        if 'pid' in state:
            self.pid = state['pid']

    def get_state(self):
        """add pid to state"""
        state = super(RootlessSpawner, self).get_state()
        if self.pid:
            state['pid'] = self.pid
        return state

    def clear_state(self):
        """clear pid state"""
        super(RootlessSpawner, self).clear_state()
        self.pid = 0

    def get_env(self):
        """Add user environment variables"""
        env = super().get_env()
        return env

    @gen.coroutine
    def start(self):
        """Start the process"""
        self.port = random_port()
        cmd = []
        env = self.get_env()

        cmd.extend(self.cmd)
        cmd.extend(self.get_args())

        self.log.info("Spawning %s", ' '.join(pipes.quote(s) for s in cmd))
        try:
            self.proc = Popen(cmd, env=env,
                start_new_session=True, # don't forward signals
            )
        except PermissionError:
            # use which to get abspath
            script = shutil.which(cmd[0]) or cmd[0]
            self.log.error("Permission denied trying to run %r. Does %s have access to this file?",
                script, self.user.name,
            )
            raise

        self.pid = self.proc.pid
        return (self.ip or '127.0.0.1', self.port)

    @gen.coroutine
    def poll(self):
        """Poll the process"""
        # if we started the process, poll with Popen
        if self.proc is not None:
            status = self.proc.poll()
            if status is not None:
                # clear state if the process is done
                self.clear_state()
            return status

        # if we resumed from stored state,
        # we don't have the Popen handle anymore, so rely on self.pid

        if not self.pid:
            # no pid, not running
            self.clear_state()
            return 0

        # send signal 0 to check if PID exists
        # this doesn't work on Windows, but that's okay because we don't support Windows.
        alive = yield self._signal(0)
        if not alive:
            self.clear_state()
            return 0
        else:
            return None

    @gen.coroutine
    def _signal(self, sig):
        try:
            os.kill(self.pid, sig)
        except OSError as e:
            if e.errno == errno.ESRCH:
                return False # process is gone
            else:
                raise
        return True # process exists

    @gen.coroutine
    def stop(self, now=False):
        """stop the subprocess

        if `now`, skip waiting for clean shutdown
        """
        if not now:
            status = yield self.poll()
            if status is not None:
                return
            self.log.debug("Interrupting %i", self.pid)
            yield self._signal(signal.SIGINT)
            yield self.wait_for_death(self.INTERRUPT_TIMEOUT)

        # clean shutdown failed, use TERM
        status = yield self.poll()
        if status is not None:
            return
        self.log.debug("Terminating %i", self.pid)
        yield self._signal(signal.SIGTERM)
        yield self.wait_for_death(self.TERM_TIMEOUT)

        # TERM failed, use KILL
        status = yield self.poll()
        if status is not None:
            return
        self.log.debug("Killing %i", self.pid)
        yield self._signal(signal.SIGKILL)
        yield self.wait_for_death(self.KILL_TIMEOUT)

        status = yield self.poll()
        if status is None:
            # it all failed, zombie process
            self.log.warning("Process %i never died", self.pid)
