import ast
from pathlib import Path
import unittest
import test_exam_arrangement_routes as fixtures
from services.choice_exam_service import ensure_schema


class AdminProgressTests(unittest.TestCase):
    def test_saved_choices_and_incorrect_numeric_answers_count_as_answered(self):
        fixture=fixtures.ArrangementRouteTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        db=fixture.db
        db.executescript('''
            ALTER TABLE user_responses ADD COLUMN template_id INT;
            ALTER TABLE user_responses ADD COLUMN attempt_count INT;
            ALTER TABLE user_responses ADD COLUMN is_correct INT;
            ALTER TABLE user_responses ADD COLUMN time_taken REAL;
            ALTER TABLE user_responses ADD COLUMN response_time TEXT;
            ALTER TABLE user_responses ADD COLUMN user_answer REAL;
            ALTER TABLE user_responses ADD COLUMN problem_text TEXT;
            UPDATE user_responses SET template_id=101,attempt_count=1,is_correct=0,user_answer=0,problem_text='numeric zero';
            CREATE TABLE problem_templates(id INT,generation_strategy TEXT);
            INSERT INTO problem_templates VALUES(101,NULL),(201,'{"mode":"choice_v1"}'),(202,'{"mode":"choice_v1"}');
            INSERT INTO exam_user_questions VALUES(9,7,1,201),(9,7,1,202);
            CREATE TABLE exam_secure_sessions(exam_id INT,user_id INT,exit_confirmed_at TEXT);
        ''')
        ensure_schema(fixtures.Cursor(db))
        db.execute("INSERT INTO exam_choice_drafts VALUES(9,1,201,'[1]',5)")
        db.execute('INSERT INTO exam_choice_sessions VALUES(9,1,1)')
        tree=ast.parse(Path('routes/admin.py').read_text(encoding='utf-8-sig'))
        query=next(n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str) and 'AS answered_count' in n.value)
        cur=fixtures.Cursor(db);cur.execute(query,(9,7,9,7,9,7,9,7));row=cur.fetchone()
        self.assertEqual(row['answered_count'],2)
        self.assertEqual(row['completed_count'],0)
        self.assertTrue(row['submitted']);self.assertFalse(row['grading_done'])
        self.assertTrue(row['has_choices'])
        # Auto grading an unanswered choice must not turn it into an answered one.
        db.execute("INSERT INTO user_responses(exam_id,batch_id,user_id,template_id,attempt_count,is_correct,user_answer,problem_text) VALUES(9,7,1,202,1,0,0,'<span data-choice-question=\"single_choice\"></span>')")
        cur.execute(query,(9,7,9,7,9,7,9,7));self.assertEqual(cur.fetchone()['answered_count'],2)
