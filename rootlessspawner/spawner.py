""" A Spawner for JupyterHub to allow users to execute Jupyter without involving root, sudo, or OS accounts. """

# Copyright (c) Christopher H Smith <chris@binc.jp>
# Distributed under the terms of the Modified BSD License.

import pipes
import shutil
from tornado import gen
from subprocess import Popen
from jupyterhub.spawner import LocalProcessSpawner
from jupyterhub.utils import random_port

class RootlessSpawner(LocalProcessSpawner):

    def get_env(self):
        return super().get_env()

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
