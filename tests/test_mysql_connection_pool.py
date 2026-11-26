import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from mysql.connector.errors import PoolError
from services.mysql_connection_pool import MySQLConnectionPool


class ConnectionPoolTests(unittest.TestCase):
    def test_rollback_and_session_reset_complete_before_reuse(self):
        raw = Mock()
        events = []
        raw.rollback.side_effect = lambda: events.append('rollback')
        raw.reset_session.side_effect = lambda: events.append('reset')
        factory = Mock(return_value=raw)
        pool = MySQLConnectionPool(factory, 1)
        first = pool.get_connection()
        first.close()
        first.close()  # Must not enqueue the same raw connection twice.
        self.assertEqual(events, ['rollback', 'reset'])
        with self.assertRaises(PoolError):
            first.cursor()
        second = pool.get_connection()
        self.assertIs(second._connection, raw)
        factory.assert_called_once()
        second.close()

    def test_blocked_reset_does_not_block_borrowing_another_idle_connection(self):
        first_raw, second_raw = Mock(), Mock()
        pool = MySQLConnectionPool(Mock(side_effect=[first_raw, second_raw]), 2)
        first, second = pool.get_connection(), pool.get_connection()
        second.close()
        entered, release = threading.Event(), threading.Event()

        def slow_reset():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test timed out')

        first_raw.reset_session.side_effect = slow_reset
        with ThreadPoolExecutor(max_workers=2) as workers:
            closing = workers.submit(first.close)
            try:
                self.assertTrue(entered.wait(2))
                borrowed = workers.submit(pool.get_connection).result(timeout=1)
                self.assertIs(borrowed._connection, second_raw)
                self.assertFalse(closing.done())
                borrowed.close()
            finally:
                release.set()
            closing.result(timeout=2)

    def test_blocked_connect_does_not_hold_pool_lock(self):
        entered, release = threading.Event(), threading.Event()

        def slow_factory():
            if threading.current_thread().name.endswith('_0'):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('test timed out')
            return Mock()

        pool = MySQLConnectionPool(slow_factory, 2)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='connect-test') as workers:
            first = workers.submit(pool.get_connection)
            try:
                self.assertTrue(entered.wait(2))
                second = workers.submit(pool.get_connection).result(timeout=1)
                self.assertFalse(first.done())
                second.close()
            finally:
                release.set()
            first.result(timeout=2).close()

    def test_creation_reserves_slots_and_never_exceeds_limit(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def factory():
            calls.append(1)
            entered.set()
            release.wait(3)
            return Mock()

        pool = MySQLConnectionPool(factory, 1, wait_seconds=0)
        with ThreadPoolExecutor(max_workers=1) as workers:
            first = workers.submit(pool.get_connection)
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(PoolError):
                    pool.get_connection()
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
            first.result(timeout=2).close()

    def test_failed_connect_releases_reserved_capacity(self):
        factory = Mock(side_effect=[RuntimeError('connect failed'), Mock()])
        pool = MySQLConnectionPool(factory, 1, wait_seconds=0)
        with self.assertRaisesRegex(RuntimeError, 'connect failed'):
            pool.get_connection()
        pool.get_connection().close()
        self.assertEqual(factory.call_count, 2)

    def test_failed_cleanup_discards_connection_instead_of_leaking_state(self):
        for stage in ('rollback', 'reset_session'):
            with self.subTest(stage=stage):
                bad, good = Mock(), Mock()
                getattr(bad, stage).side_effect = RuntimeError('cleanup failed')
                factory = Mock(side_effect=[bad, good])
                pool = MySQLConnectionPool(factory, 1, wait_seconds=0)
                with self.assertLogs('services.mysql_connection_pool', level='WARNING'):
                    pool.get_connection().close()
                bad.close.assert_called_once()
                lease = pool.get_connection()
                self.assertIs(lease._connection, good)
                lease.close()

    def test_waiting_borrower_is_notified_on_return(self):
        pool = MySQLConnectionPool(Mock(return_value=Mock()), 1, wait_seconds=2)
        lease = pool.get_connection()
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(pool.get_connection)
            lease.close()
            future.result(timeout=1).close()

    def test_rollback_remains_enabled_when_reset_is_explicitly_disabled(self):
        raw = Mock()
        pool = MySQLConnectionPool(lambda: raw, 1, reset_session=False)
        with pool.get_connection() as lease:
            lease.autocommit = True
            self.assertIs(raw.autocommit, True)
            lease.commit()
        raw.commit.assert_called_once()
        raw.rollback.assert_called_once()
        raw.reset_session.assert_not_called()


if __name__ == '__main__':
    unittest.main()
