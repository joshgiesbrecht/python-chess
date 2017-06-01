# -*- coding: utf-8 -*-
#
# This file is part of the python-chess library.
# Copyright (C) 2012-2017 Niklas Fiekas <niklas.fiekas@backscattering.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import logging
import threading
import os
import sys
import signal
import platform

try:
    import backport_collections as collections
except ImportError:
    import collections

try:
    import queue
except ImportError:
    import Queue as queue

if os.name == "posix" and sys.version_info[0] < 3:
    try:
        import subprocess32 as subprocess
    except ImportError:
        import subprocess
else:
    import subprocess


FUTURE_POLL_TIMEOUT = 0.1 if platform.system() == "Windows" else 60

LOGGER = logging.getLogger(__name__)


class EngineTerminatedException(Exception):
    """The engine has been terminated."""
    pass


class EngineStateException(Exception):
    """Unexpected engine state."""
    pass


class MockProcess(object):
    def __init__(self, engine):
        self.engine = engine
        self._expectations = collections.deque()
        self._is_dead = threading.Event()
        self._std_streams_closed = False

        self.engine.on_process_spawned(self)

        self._send_queue = queue.Queue()
        self._send_thread = threading.Thread(target=self._send_thread_target)
        self._send_thread.daemon = True
        self._send_thread.start()

    def _send_thread_target(self):
        while not self._is_dead.is_set():
            line = self._send_queue.get()
            if line is not None:
                self.engine.on_line_received(line)
            self._send_queue.task_done()

    def expect(self, expectation, responses=()):
        self._expectations.append((expectation, responses))

    def assert_done(self):
        assert not self._expectations, "pending expectations: {0}".format(self._expectations)

    def assert_terminated(self):
        self.assert_done()
        assert self._is_dead.is_set()

    def is_alive(self):
        return not self._is_dead.is_set()

    def terminate(self):
        self._is_dead.set()
        self._send_queue.put(None)
        self.engine.on_terminated()

    def kill(self):
        self._is_dead.set()
        self._send_queue.put(None)
        self.engine.on_terminated()

    def send_line(self, string):
        assert self.is_alive()

        assert self._expectations, "unexpected: {0}".format(string)
        expectation, responses = self._expectations.popleft()
        assert expectation == string, "expected: {0}, got {1}".format(expectation, string)

        for response in responses:
            self._send_queue.put(response)

    def wait_for_return_code(self):
        self._is_dead.wait()
        return 0

    def pid(self):
        return None

    def __repr__(self):
        return "<MockProcess at {0}>".format(hex(id(self)))


class PopenProcess(object):
    def __init__(self, engine, command, **kwargs):
        self.engine = engine

        self._receiving_thread = threading.Thread(target=self._receiving_thread_target)
        self._receiving_thread.daemon = True
        self._stdin_lock = threading.Lock()

        self.engine.on_process_spawned(self)

        popen_args = {
            "stdout": subprocess.PIPE,
            "stdin": subprocess.PIPE,
            "bufsize": 1,  # Line buffering
            "universal_newlines": True,
        }
        popen_args.update(kwargs)
        self.process = subprocess.Popen(command, **popen_args)

        self._receiving_thread.start()

    def _receiving_thread_target(self):
        while True:
            line = self.process.stdout.readline()
            if not line:
                # Stream closed.
                break

            self.engine.on_line_received(line.rstrip())

        # Close file descriptors.
        self.process.stdout.close()
        with self._stdin_lock:
            self.process.stdin.close()

        # Ensure the process is terminated (not just the in/out streams).
        if self.is_alive():
            self.terminate()
            self.wait_for_return_code()

        self.engine.on_terminated()

    def is_alive(self):
        return self.process.poll() is None

    def terminate(self):
        self.process.terminate()

    def kill(self):
        self.process.kill()

    def send_line(self, string):
        with self._stdin_lock:
            self.process.stdin.write(string + "\n")
            self.process.stdin.flush()

    def wait_for_return_code(self):
        self.process.wait()
        return self.process.returncode

    def pid(self):
        return self.process.pid

    def __repr__(self):
        return "<PopenProcess at {0} (pid={1})>".format(hex(id(self)), self.pid())


class SpurProcess(object):
    def __init__(self, engine, shell, command):
        self.engine = engine
        self.shell = shell

        self._stdout_buffer = []

        self._result = None

        self._waiting_thread = threading.Thread(target=self._waiting_thread_target)
        self._waiting_thread.daemon = True

        self.engine.on_process_spawned(self)
        self.process = self.shell.spawn(command, store_pid=True, allow_error=True, stdout=self)
        self._waiting_thread.start()

    def write(self, byte):
        # Interally called whenever a byte is received.
        if byte == b"\r":
            pass
        elif byte == b"\n":
            self.engine.on_line_received(b"".join(self._stdout_buffer).decode("utf-8"))
            del self._stdout_buffer[:]
        else:
            self._stdout_buffer.append(byte)

    def _waiting_thread_target(self):
        self._result = self.process.wait_for_result()
        self.engine.on_terminated()

    def is_alive(self):
        return self.process.is_running()

    def terminate(self):
        self.process.send_signal(signal.SIGTERM)

    def kill(self):
        self.process.send_signal(signal.SIGKILL)

    def send_line(self, string):
        self.process.stdin_write(string.encode("utf-8"))
        self.process.stdin_write(b"\n")

    def wait_for_return_code(self):
        return self.process.wait_for_result().return_code

    def pid(self):
        return self.process.pid

    def __repr__(self):
        return "<SpurProcess at {0} (pid={1})>".format(hex(id(self)), self.pid())
