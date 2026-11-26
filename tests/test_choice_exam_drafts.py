import json
import sqlite3
import unittest
import ast
from unittest.mock import patch
from mysql.connector import Error as MySQLError
from pathlib import Path
import test_exam_arrangement_routes as fixture
from services import choice_exam_service as service


class ChoiceDraftTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''
          CREATE TABLE problem_templates(id INT,paper_id INT,template_name TEXT,problem_text TEXT,generation_strategy TEXT);
          CREATE TABLE exam_user_questions(exam_id INT,user_id INT,template_id INT,batch_id INT,display_number INT);
          CREATE TABLE user_responses(user_id INT,template_id INT,problem_text TEXT,user_answer REAL,correct_answer REAL,
            is_correct INT,attempt_count INT,time_taken REAL,answer_index INT,paper_id INT,switch_count INT,exam_id INT,batch_id INT);
        ''')
        self.connect=lambda:fixture.Connection(self.db)
        cursor=fixture.Cursor(self.db);service.ensure_schema(cursor);cursor.close()
        definition=dict(mode='choice_v1',question_type='multiple_choice',options=[dict(id=1,text='甲'),dict(id=2,text='乙')],correct_ids=[1,2])
        self.db.executemany('INSERT INTO problem_templates VALUES(?,?,?,?,?)',[(1,10,'choice1','题干1',json.dumps(definition)),(2,20,'choice2','题干2',json.dumps(definition)),(3,20,'numeric','计算题',None)])
        self.db.executemany('INSERT INTO exam_user_questions VALUES(9,7,?,4,?)',[(1,1),(2,2),(3,3)])
        self.db.commit()

    def test_drafts_can_be_updated_without_any_grading_records(self):
        service.save_draft(self.connect,9,7,1,[1],5)
        service.save_draft(self.connect,9,7,1,[1,2],8)
        self.assertEqual(service.state(self.connect,9,7),(False,{1:[1,2]}))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM user_responses').fetchone()[0],0)

    def test_hand_in_grades_once_locks_answers_and_leaves_numeric_records_untouched(self):
        self.db.execute('INSERT INTO user_responses(user_id,template_id,exam_id,attempt_count,is_correct) VALUES(7,3,9,1,1)');self.db.commit()
        service.save_draft(self.connect,9,7,1,[1,2],10)
        service.save_draft(self.connect,9,7,2,[1],12)
        service.hand_in(self.connect,9,7);service.hand_in(self.connect,9,7)
        rows=self.db.execute('SELECT template_id,is_correct FROM user_responses ORDER BY template_id').fetchall()
        self.assertEqual([tuple(r) for r in rows],[(1,1),(2,0),(3,1)])
        self.assertTrue(service.state(self.connect,9,7)[0])
        with self.assertRaises(ValueError):service.save_draft(self.connect,9,7,2,[1,2],13)

    def test_unanswered_choices_are_graded_incorrect_at_explicit_hand_in(self):
        service.hand_in(self.connect,9,7)
        self.assertEqual(self.db.execute('SELECT SUM(is_correct) FROM user_responses').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM user_responses').fetchone()[0],2)

    def test_failure_rolls_back_all_grades_and_submit_lock(self):
        self.db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON user_responses WHEN NEW.template_id=2 BEGIN SELECT RAISE(ABORT,'simulated failure'); END;")
        service.save_draft(self.connect,9,7,1,[1,2],10)
        with self.assertRaises(sqlite3.DatabaseError):service.hand_in(self.connect,9,7)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM user_responses').fetchone()[0],0)
        self.assertFalse(service.state(self.connect,9,7)[0])
        self.assertEqual(service.state(self.connect,9,7)[1],{1:[1,2]})

    def test_drafts_are_student_and_exam_scoped(self):
        with self.assertRaises(ValueError):service.save_draft(self.connect,9,8,1,[1],1)
        service.save_draft(self.connect,9,7,1,[1],1)
        self.assertEqual(service.state(self.connect,10,7)[1],{})
        self.assertEqual(service.state(self.connect,9,8)[1],{})

    def failing_draft_connections(self, failures, errno=1213):
        db = self.db
        attempts = []
        statements = []

        class FailingCursor(fixture.Cursor):
            def execute(self, sql, params=()):
                statements.append(sql)
                result = super().execute(sql, params)
                if sql.lstrip().startswith('INSERT INTO exam_choice_drafts'):
                    attempts.append(1)
                    if len(attempts) <= failures:
                        # Fail after writing, to exercise full transaction rollback.
                        raise MySQLError('simulated transient conflict', errno=errno)
                return result

        class Connection(fixture.Connection):
            def cursor(self, **kwargs):
                return FailingCursor(db)

        return lambda: Connection(db), attempts, statements

    def test_deadlock_rolls_back_then_retries_atomic_save(self):
        connect, attempts, statements = self.failing_draft_connections(1)
        with patch.object(service.time, 'sleep') as sleep:
            service.save_draft(connect,9,7,1,[1,2],8)
        self.assertEqual(len(attempts),2)
        sleep.assert_called_once()
        self.assertEqual(service.state(self.connect,9,7),(False,{1:[1,2]}))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM exam_choice_drafts').fetchone()[0],1)
        self.assertFalse(any('DELETE FROM exam_choice_drafts' in sql for sql in statements))

    def test_exhausted_retries_preserve_previous_saved_answer(self):
        service.save_draft(self.connect,9,7,1,[1],5)
        connect, attempts, _ = self.failing_draft_connections(3)
        with patch.object(service.time,'sleep') as sleep, self.assertRaises(MySQLError):
            service.save_draft(connect,9,7,1,[1,2],9)
        self.assertEqual(len(attempts),3)
        self.assertEqual(sleep.call_count,2)
        self.assertEqual(service.state(self.connect,9,7),(False,{1:[1]}))
        self.assertEqual(self.db.execute('SELECT time_taken FROM exam_choice_drafts').fetchone()[0],5)

    def test_retry_rechecks_hand_in_lock_instead_of_overwriting_locked_answer(self):
        service.save_draft(self.connect,9,7,1,[1],5)
        connect, attempts, _ = self.failing_draft_connections(1)
        with patch.object(service.time,'sleep',side_effect=lambda _:service.lock_answers(self.connect,9,7)):
            with self.assertRaisesRegex(ValueError,'已交卷'):
                service.save_draft(connect,9,7,1,[1,2],9)
        self.assertEqual(len(attempts),1)
        self.assertEqual(service.state(self.connect,9,7),(True,{1:[1]}))

    def test_lock_timeout_retries_but_other_database_errors_do_not(self):
        for errno, expected_attempts in ((1205,2),(1062,1)):
            with self.subTest(errno=errno), patch.object(service.time,'sleep'):
                connect, attempts, _ = self.failing_draft_connections(1,errno)
                if errno == 1205:
                    service.save_draft(connect,9,7,1,[1],5)
                else:
                    with self.assertRaises(MySQLError):
                        service.save_draft(connect,9,7,1,[1,2],9)
                self.assertEqual(len(attempts),expected_attempts)

    def test_retry_rechecks_question_membership(self):
        connect, attempts, _ = self.failing_draft_connections(1)

        def remove_question(_):
            self.db.execute('DELETE FROM exam_user_questions WHERE template_id=1')
            self.db.commit()

        with patch.object(service.time,'sleep',side_effect=remove_question):
            with self.assertRaisesRegex(ValueError,'题目不属于'):
                service.save_draft(connect,9,7,1,[1],5)
        self.assertEqual(len(attempts),1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM exam_choice_drafts').fetchone()[0],0)

    def test_progress_counts_saved_choices_without_disclosing_correctness(self):
        tree=ast.parse(Path('app.py').read_text(encoding='utf-8'))
        func=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='get_completion_status_map')
        ns=dict(get_db_connection=self.connect, choice_exam_service=service,
                get_user_exam_problem_display_info=lambda *args,**kw:{1:dict(is_choice=True),2:dict(is_choice=True),3:dict(is_choice=False)})
        exec(compile(ast.Module(body=[func],type_ignores=[]),'app.py','exec'),ns)
        progress=lambda:ns[func.name](7,10,[1,2,3],9)
        self.assertEqual(progress(),{1:False,2:False,3:False})
        service.save_draft(self.connect,9,7,1,[1],1)  # An incorrect choice is still saved.
        self.assertEqual(progress(),{1:True,2:False,3:False})
        self.db.execute('INSERT INTO user_responses(user_id,template_id,exam_id,attempt_count,is_correct) VALUES(7,3,9,1,1)');self.db.commit()
        self.assertEqual(progress(),{1:True,2:False,3:True})
        service.hand_in(self.connect,9,7)
        self.assertEqual(progress(),{1:True,2:True,3:True})
