"""Bounded connection reuse without holding a pool lock during MySQL I/O."""
import logging
import threading
import time

from mysql.connector.errors import PoolError

logger = logging.getLogger(__name__)


class _ConnectionLease:
    """Keep the existing cursor/commit/rollback/close interface for callers."""

    def __init__(self, pool, connection):
        self._pool = pool
        self._connection = connection
        self._close_lock = threading.Lock()

    def __getattr__(self, name):
        connection = self._connection
        if connection is None:
            raise PoolError('Connection has already been returned')
        return getattr(connection, name)

    def __setattr__(self, name, value):
        if name.startswith('_'):
            object.__setattr__(self, name, value)
        else:
            setattr(self._connection, name, value)

    def close(self):
        with self._close_lock:
            connection = self._connection
            if connection is None:
                return
            self._connection = None
        self._pool.release(connection)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class MySQLConnectionPool:
    def __init__(self, connect, pool_size, reset_session=True, wait_seconds=0.15):
        self._connect = connect
        self.pool_size = pool_size
        self._reset_session = reset_session
        self._wait_seconds = wait_seconds
        self._condition = threading.Condition()
        self._idle = []
        # Includes connections being opened, checked out, or cleaned for reuse.
        self._allocated = 0

    def get_connection(self):
        deadline = time.monotonic() + self._wait_seconds
        with self._condition:
            while True:
                if self._idle:
                    connection = self._idle.pop()
                    break
                if self._allocated < self.pool_size:
                    self._allocated += 1
                    connection = None
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PoolError('Failed getting connection; pool exhausted')
                self._condition.wait(remaining)

        # DNS, TCP, TLS and authentication must never run under the pool lock.
        if connection is None:
            try:
                connection = self._connect()
            except BaseException:
                self._discard_slot()
                raise
        return _ConnectionLease(self, connection)

    def _discard_slot(self):
        with self._condition:
            self._allocated -= 1
            self._condition.notify()

    def release(self, connection):
        # Finish transactions even when full session reset is explicitly disabled.
        # A connection stays unavailable until all cleanup has completed.
        try:
            connection.rollback()
            if self._reset_session:
                connection.reset_session()
        except Exception:
            logger.warning('Discarding a MySQL connection after cleanup failed', exc_info=True)
            try:
                connection.close()
            except Exception:
                pass
            finally:
                self._discard_slot()
            return
        with self._condition:
            self._idle.append(connection)
            self._condition.notify()
