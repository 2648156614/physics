from datetime import datetime, timedelta
import unittest

from services.exam_batch_service import classify_exam_student_status


NOW = datetime(2026, 9, 15, 10, 0, 0)


def classify(**overrides):
    values = {
        'entered': False,
        'completed_count': 0,
        'total_problems': 8,
        'start_time': NOW - timedelta(hours=1),
        'end_time': NOW + timedelta(hours=1),
        'now': NOW,
    }
    values.update(overrides)
    return classify_exam_student_status(**values)


class ExamBatchStatusTests(unittest.TestCase):
    def test_unopened_and_not_started_students_are_distinguished(self):
        self.assertEqual(classify(start_time=NOW + timedelta(minutes=1)), 'not_open')
        self.assertEqual(classify(start_time=None), 'not_started')

    def test_entered_student_is_in_progress_before_end(self):
        self.assertEqual(classify(entered=True, completed_count=3), 'in_progress')

    def test_completed_status_has_priority_at_or_after_end(self):
        self.assertEqual(
            classify(entered=True, completed_count=8, end_time=NOW),
            'completed',
        )

    def test_ended_batch_distinguishes_absent_and_incomplete(self):
        self.assertEqual(classify(end_time=NOW), 'absent')
        self.assertEqual(
            classify(entered=True, completed_count=3, end_time=NOW),
            'ended_incomplete',
        )


if __name__ == '__main__':
    unittest.main()
