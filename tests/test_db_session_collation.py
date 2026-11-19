import ast
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class SessionCollationTests(unittest.TestCase):
    def setUp(self):
        class DatabaseError(Exception):
            pass
        self.error = DatabaseError
        self.conn = Mock()
        self.pool = Mock()
        self.pool.get_connection.return_value = self.conn
        self.namespace = dict(_get_pool=lambda:self.pool, mysql=SimpleNamespace(
            connector=SimpleNamespace(Error=DatabaseError)), PoolError=type('PoolError',(Exception,),{}),
            logger=Mock(), time=SimpleNamespace(sleep=Mock()), POOL_ACQUIRE_RETRIES=2,
            POOL_ACQUIRE_DELAY_SECONDS=0, DB_POOL_SIZE=1, MYSQL_CONNECTOR_POOL_MAX_SIZE=32)
        tree = ast.parse((Path(__file__).parents[1]/'db.py').read_text(encoding='utf-8'))
        function = next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='get_db_connection')
        exec(compile(ast.Module(body=[function],type_ignores=[]),'db.py','exec'),self.namespace)

    def test_sets_session_after_ping_before_returning_connection(self):
        events=[]
        self.conn.ping.side_effect=lambda **kwargs:events.append('ping')
        self.conn.cursor.return_value.execute.side_effect=lambda sql:events.append(sql)
        self.assertIs(self.namespace['get_db_connection'](),self.conn)
        self.assertEqual(events,['ping','SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci'])
        self.conn.cursor.return_value.close.assert_called_once()

    def test_reapplies_on_every_pool_checkout(self):
        self.namespace['get_db_connection']()
        self.namespace['get_db_connection']()
        self.assertEqual(self.conn.cursor.return_value.execute.call_count,2)

    def test_failed_session_setup_does_not_return_bad_connection(self):
        self.conn.cursor.return_value.execute.side_effect=self.error('session setup failed')
        self.assertIsNone(self.namespace['get_db_connection']())
        self.assertEqual(self.conn.close.call_count,2)
        self.assertEqual(self.conn.cursor.return_value.close.call_count,2)


if __name__=='__main__':
    unittest.main()
