import unittest
from unittest.mock import Mock, patch
import test_choice_exam_drafts as drafts
from services import exam_auto_submit_service as worker
from services import choice_exam_service


class AutoSubmitTests(unittest.TestCase):
    def test_manual_hand_in_only_locks_until_deadline(self):
        choice_exam_service.save_draft(self.connect,9,7,1,[1,2],8)
        choice_exam_service.lock_answers(self.connect,9,7)
        self.assertTrue(choice_exam_service.state(self.connect,9,7)[0])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM user_responses').fetchone()[0],0)
        self.assertEqual(worker.process_expired(self.connect,self.callback),(1,1))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM user_responses').fetchone()[0],2)

    def test_next_deadline_tracks_teacher_changes_without_fixed_polling(self):
        self.assertIsNone(worker.next_deadline(self.connect))
        self.db.execute("UPDATE exam_batches SET end_time=datetime('now','+1 hour')");self.db.commit()
        deadline=worker.next_deadline(self.connect)
        self.assertIsNotNone(deadline)
        self.db.execute("UPDATE exam_batches SET end_time=datetime('now','+2 hours')");self.db.commit()
        self.assertGreater(worker.next_deadline(self.connect),deadline)
        worker._schedule_changed.clear();worker.notify_schedule_changed()
        self.assertTrue(worker._schedule_changed.is_set())

    def test_rate_limit_hook_runs_for_each_student(self):
        pace=Mock(return_value=False)
        self.assertEqual(worker.process_expired(self.connect,self.callback,limit=10,pace=pace),(1,1))
        pace.assert_called_once()

    def setUp(self):
        self.fixture=drafts.ChoiceDraftTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db=self.fixture.db;self.connect=self.fixture.connect;self.callback=Mock()
        self.db.executescript('''
            CREATE TABLE exams(id INT,status TEXT,end_time TEXT);
            CREATE TABLE exam_batches(id INT,exam_id INT,status TEXT,end_time TEXT);
            CREATE TABLE exam_batch_students(exam_id INT,user_id INT,batch_id INT,is_active INT);
            INSERT INTO exams VALUES(9,'published',datetime('now','+1 hour'));
            INSERT INTO exam_batches VALUES(4,9,'active',datetime('now','-1 minute'));
            INSERT INTO exam_batch_students VALUES(9,7,4,1);
            UPDATE exam_auto_submit_settings SET enabled_after=datetime('now','-1 day');
        ''')

    def test_expired_exam_grades_saved_drafts_and_is_idempotent(self):
        choice_exam_service.save_draft(self.connect,9,7,1,[1,2],8)
        self.assertEqual(worker.process_expired(self.connect,self.callback),(1,1))
        self.assertTrue(choice_exam_service.state(self.connect,9,7)[0])
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT template_id,is_correct FROM user_responses ORDER BY template_id')],[(1,1),(2,0)])
        self.callback.assert_called_once_with(7,9)
        self.assertEqual(worker.process_expired(self.connect,self.callback),(0,0))

    def test_future_no_deadline_and_historical_exams_are_not_submitted(self):
        for end in ["datetime('now','+1 hour')","NULL","'2000-01-01 00:00:00'"]:
            self.db.execute('UPDATE exam_batches SET end_time='+end)
            if end=='NULL':self.db.execute('UPDATE exams SET end_time=NULL')
            self.db.commit()
            self.assertEqual(worker.process_expired(self.connect,self.callback),(0,0))

    def test_deadline_is_rechecked_if_teacher_extends_exam(self):
        real=choice_exam_service.hand_in
        def extended(*args,**kwargs):
            self.db.execute("UPDATE exam_batches SET end_time=datetime('now','+1 hour')");self.db.commit()
            return real(*args,**kwargs)
        with patch.object(worker,'hand_in',side_effect=extended):
            self.assertEqual(worker.process_expired(self.connect,self.callback),(1,0))
        self.assertFalse(choice_exam_service.state(self.connect,9,7)[0])

    def test_failed_submission_is_retried_without_losing_drafts(self):
        choice_exam_service.save_draft(self.connect,9,7,1,[1,2],8)
        with patch.object(worker,'hand_in',side_effect=RuntimeError('temporary outage')),patch.object(worker.logger,'exception'):
            self.assertEqual(worker.process_expired(self.connect,self.callback),(1,0))
        self.assertEqual(worker.process_expired(self.connect,self.callback),(1,1))

    def test_numeric_only_exam_records_submission_without_changing_numeric_answers(self):
        self.db.execute('UPDATE problem_templates SET generation_strategy=NULL');self.db.commit()
        self.assertEqual(worker.process_expired(self.connect,self.callback),(1,1))
        self.assertTrue(choice_exam_service.state(self.connect,9,7)[0])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM user_responses').fetchone()[0],0)

    def test_student_who_never_entered_is_not_submitted(self):
        self.db.execute('DELETE FROM exam_user_questions');self.db.commit()
        self.assertEqual(worker.process_expired(self.connect,self.callback),(0,0))
