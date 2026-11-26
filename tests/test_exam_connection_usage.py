"""Exercise the real helpers with a one-slot pool and metadata cache misses."""
import ast
import contextlib
import io
from pathlib import Path
import unittest
from unittest.mock import Mock

import mysql.connector
from services.mysql_connection_pool import MySQLConnectionPool


class ExamConnectionUsageTests(unittest.TestCase):
    def load_helper(self, name, namespace):
        tree = ast.parse(Path('app.py').read_text(encoding='utf-8'))
        # app.py retains a legacy definition of update_user_completion_status.
        function = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name][-1]
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'app.py', 'exec'), namespace)
        return namespace[name]

    def test_numeric_save_does_not_hold_write_connection_during_assignment_read(self):
        raw = Mock()
        raw.cursor.return_value.fetchone.return_value = None  # A missing user ends the write path.
        raw.is_connected.return_value = False
        pool = MySQLConnectionPool(lambda: raw, 1, wait_seconds=0)
        metadata_reads = []

        def assignment(*args):
            # The real assignment helper borrows its own connection on a cache miss.
            with pool.get_connection():
                metadata_reads.append(1)
            return {}

        save = self.load_helper('save_user_response', dict(
            get_db_connection=pool.get_connection,
            get_user_exam_problem_display_info=assignment,
            mysql=mysql,
        ))
        with contextlib.redirect_stdout(io.StringIO()):
            result = save(7, 1, 10, 'question', [1], [1], [True], 1, 5, exam_id=9)
        self.assertEqual(result, (False, []))
        self.assertEqual(metadata_reads, [1])
        raw.cursor.return_value.close.assert_called_once()
        # Even a disconnected lease must be returned, not skipped by is_connected().
        pool.get_connection().close()

    def test_statistics_read_metadata_before_borrowing_write_connection(self):
        raw = Mock()
        raw.cursor.return_value.fetchone.side_effect = [
            {'completed_count': 0}, {'total_score': 0, 'total_time': 0},
        ]
        pool = MySQLConnectionPool(lambda: raw, 1, wait_seconds=0)
        reads = []

        def exam(*args):
            with pool.get_connection():
                reads.append('exam')
            return {'paper_id': 10}

        def assignment(*args):
            with pool.get_connection():
                reads.append('assignment')
            return {1: {'is_choice': False}}

        update = self.load_helper('update_user_completion_status', dict(
            get_db_connection=pool.get_connection, get_exam_by_id=exam,
            get_user_exam_problem_display_info=assignment,
        ))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(update(7, 9), False)
        self.assertEqual(reads, ['exam', 'assignment'])
        raw.commit.assert_called_once()
        raw.cursor.return_value.close.assert_called_once()
        pool.get_connection().close()
