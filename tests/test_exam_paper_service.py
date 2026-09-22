import unittest

from services.exam_paper_service import (
    ensure_exam_paper_enabled,
    paper_has_unfinished_exam,
)


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executions = []

    def execute(self, query, params=None):
        self.executions.append((query, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class ExamPaperLifecycleTests(unittest.TestCase):
    def test_unfinished_exam_blocks_paper_close(self):
        cursor = FakeCursor([{'locked': 1}])

        self.assertTrue(paper_has_unfinished_exam(cursor, 7))
        query, params = cursor.executions[0]
        self.assertIn("e.status = 'published'", query)
        self.assertIn("b.status = 'active'", query)
        self.assertIn('b.end_time > NOW()', query)
        self.assertEqual(params, (7,))

    def test_finished_exam_allows_paper_close(self):
        cursor = FakeCursor([{'locked': 0}])

        self.assertFalse(paper_has_unfinished_exam(cursor, 7))

    def test_disabled_paper_is_enabled_for_exam_setup(self):
        cursor = FakeCursor([{'id': 7, 'is_enabled': 0}])

        self.assertTrue(ensure_exam_paper_enabled(cursor, 7))
        self.assertEqual(len(cursor.executions), 2)
        self.assertIn('FOR UPDATE', cursor.executions[0][0])
        self.assertIn('SET is_enabled = TRUE', cursor.executions[1][0])

    def test_enabled_paper_is_left_unchanged(self):
        cursor = FakeCursor([{'id': 7, 'is_enabled': 1}])

        self.assertFalse(ensure_exam_paper_enabled(cursor, 7))
        self.assertEqual(len(cursor.executions), 1)

    def test_missing_paper_is_rejected(self):
        cursor = FakeCursor([None])

        with self.assertRaisesRegex(ValueError, '题库不存在'):
            ensure_exam_paper_enabled(cursor, 99)


if __name__ == '__main__':
    unittest.main()
