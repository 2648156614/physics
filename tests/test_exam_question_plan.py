"""Exercise multi-bank policies with a transactional database and real route handlers."""
import ast
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from flask import g, has_request_context

from services.exam_question_plan_service import (
    describe_plan, freeze_plan, normalize_plan, plan_count, public_plan, select_plan_questions,
)
from services import choice_exam_service
from services.choice_question_service import choice_definition
from services.exam_assignment_service import select_exam_template_ids
from services.exam_paper_service import ensure_exam_paper_enabled, paper_has_unfinished_exam
import test_exam_arrangement_routes as arrangements
import test_seb_entry_routes as exam_routes


class QuestionPlanTests(unittest.TestCase):
    def setUp(self):
        self.templates = {
            10: [dict(id=i, status='active') for i in range(101, 106)],
            20: [dict(id=i, status='active') for i in range(201, 209)],
        }
        self.raw = [dict(paper_id=10, mandatory_ids=[101], random_count=2),
                    dict(paper_id=20, mandatory_ids=[201, 202], random_count=3)]

    def plan(self, raw=None):
        return normalize_plan(self.raw if raw is None else raw, self.templates.get)

    def test_each_bank_quota_and_mandatory_questions_for_many_students(self):
        plan = self.plan()
        self.assertEqual(plan_count(plan), 8)
        orders = set()
        for user in range(1, 101):
            selected = select_plan_questions(plan, range(101, 209), 9, user, 'test-secret')
            self.assertEqual(len(set(selected)), 8)
            self.assertTrue({101, 201, 202}.issubset(selected))
            self.assertEqual(sum(101 <= i <= 105 for i in selected), 3)
            self.assertEqual(sum(201 <= i <= 208 for i in selected), 5)
            self.assertEqual(selected, select_plan_questions(plan, range(101, 209), 9, user, 'test-secret'))
            orders.add(tuple(selected))
        self.assertGreater(len(orders), 1)

    def test_mandatory_only_and_random_only(self):
        for row in [dict(paper_id=10, mandatory_ids=[101, 102], random_count=0),
                    dict(paper_id=10, mandatory_ids=[], random_count=5)]:
            plan = self.plan([row])
            self.assertEqual(len(select_plan_questions(plan, range(101, 106), 9, 1, 's')), plan_count(plan))

    def test_reject_invalid_or_foreign_questions_and_excess_quota(self):
        invalid = [[], {}, 'not-json', [None],
                   [dict(paper_id=10, mandatory_ids=[201], random_count=0)],
                   [dict(paper_id=10, mandatory_ids=[101, 101], random_count=0)],
                   [dict(paper_id=10, mandatory_ids=[101], random_count=5)],
                   [dict(paper_id=10, mandatory_ids=[], random_count=0)],
                   [dict(paper_id=10.5, random_count=1)],
                   [dict(paper_id=True, random_count=1)],
                   [dict(paper_id=10, mandatory_ids=[101.5], random_count=0)],
                   [dict(paper_id=10, random_count=-1)],
                   [dict(paper_id=10, random_count=1.5)], self.raw + [self.raw[0]]]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.plan(raw)

    def test_metadata_edits_preserve_retired_snapshot(self):
        plan = self.plan()
        self.templates[10] = []
        self.assertEqual(normalize_plan(json.dumps(public_plan(plan)), self.templates.get, json.dumps(plan)), plan)
        with self.assertRaises(ValueError):
            select_plan_questions(plan, [101, 201], 9, 1, 's')

    def test_schema_upgrade_adds_nullable_policy_once_and_does_not_rebuild_planned_pools(self):
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'ensure_question_lifecycle_schema')
        ns = dict(choice_exam_service=choice_exam_service)
        exec(compile(ast.Module(body=[func], type_ignores=[]), 'app.py', 'exec'), ns)
        class MigrationCursor:
            def __init__(self):
                self.has_plan = False
                self.queries = []
            def execute(self, query):
                self.queries.append(query)
                if query.startswith('ALTER TABLE exams'):
                    self.has_plan = True
            def fetchone(self):
                if self.queries[-1].startswith('SHOW COLUMNS FROM exams'):
                    return ('question_plan',) if self.has_plan else None
                return ('existing-column',)
        cursor = MigrationCursor()
        ns[func.name](cursor)
        ns[func.name](cursor)
        alters = [query for query in cursor.queries if query.startswith('ALTER TABLE exams')]
        self.assertEqual(alters, ['ALTER TABLE exams ADD COLUMN question_plan LONGTEXT DEFAULT NULL'])
        for query in cursor.queries:
            if 'INSERT IGNORE INTO exam_question_pool' in query:
                self.assertIn('e.question_plan IS NULL', query)


class QuestionPlanDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = arrangements.ArrangementRouteTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.db = self.fixture.db
        self.db.executescript('''
            ALTER TABLE exams ADD COLUMN question_plan TEXT;
            ALTER TABLE exam_user_questions ADD COLUMN display_number INT;
            CREATE TABLE problem_templates(id INT PRIMARY KEY,paper_id INT,template_name TEXT,status TEXT,generation_strategy TEXT);
            CREATE TABLE exam_question_pool(exam_id INT,template_id INT,PRIMARY KEY(exam_id,template_id));
            INSERT INTO exam_papers VALUES(20,'second bank','',0);
            DELETE FROM exam_user_questions;
            DELETE FROM user_responses;
        ''')
        self.db.executemany('INSERT INTO problem_templates(id,paper_id,template_name,status) VALUES(?,?,?,?)',
                            [(i, bank, f'question {i}', 'active') for bank, ids in
                             [(10, range(101, 106)), (20, range(201, 209))] for i in ids])
        self.raw = [dict(paper_id=10, mandatory_ids=[101], random_count=2),
                    dict(paper_id=20, mandatory_ids=[201, 202], random_count=3)]
        self.namespace = self.fixture.app.view_functions['admin.admin_update_exam'].__wrapped__.__globals__
        self.namespace['get_problem_templates_by_paper'] = lambda pid, **kw: [dict(row) for row in
            self.db.execute('SELECT * FROM problem_templates WHERE paper_id=?', (pid,))]
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'replace_exam_question_pool')
        ns = dict(g=g, has_request_context=has_request_context, freeze_plan=freeze_plan,
                  ensure_exam_paper_enabled=ensure_exam_paper_enabled)
        exec(compile(ast.Module(body=[func], type_ignores=[]), 'app.py', 'exec'), ns)
        self.namespace['replace_exam_question_pool'] = ns[func.name]
        self.namespace['invalidate_exam_paper_cache'] = Mock()
        self.namespace['schedule_exam_paper_prewarm'] = Mock()
        self.db.commit()

    def enable_paper(self, cursor, paper_id):
        cursor.execute('UPDATE exam_papers SET is_enabled=1 WHERE id=%s', (paper_id,))

    def save(self, raw=None):
        return self.fixture.post_exam(question_plan=json.dumps(self.raw if raw is None else raw))

    def test_save_freezes_both_banks_and_totals_and_enables_secondary(self):
        self.assertEqual(self.save().status_code, 302)
        exam = self.fixture.exam()
        self.assertEqual(exam['question_count'], 8)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM exam_question_pool').fetchone()[0], 13)
        self.assertEqual(self.db.execute('SELECT is_enabled FROM exam_papers WHERE id=20').fetchone()[0], 1)
        self.assertEqual(describe_plan(exam['question_plan']), 'paper、second bank')
        options = self.fixture.client.get('/admin/exams/question-options?exam_id=9').get_json()
        self.assertEqual(options['selected'], self.raw)
        self.assertFalse(options['locked'])

    def test_invalid_plan_leaves_exam_and_pool_unchanged(self):
        self.save()
        before = self.fixture.exam()
        self.save([dict(paper_id=10, mandatory_ids=[201], random_count=1)])
        self.assertEqual(self.fixture.exam(), before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM exam_question_pool').fetchone()[0], 13)

    def test_create_exam_with_real_frozen_pool(self):
        response = self.fixture.client.post('/admin/exams/create', data=dict(
            name='multi-bank new exam', question_plan=json.dumps(self.raw), exam_type='normal',
            assign_type='class', assign_value='A', start_time='2026-10-19T08:00',
            end_time='2026-10-19T13:00'))
        self.assertEqual(response.status_code, 302)
        exam = self.db.execute("SELECT * FROM exams WHERE name='multi-bank new exam'").fetchone()
        self.assertIsNotNone(exam)
        self.assertEqual(exam['question_count'], 8)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM exam_question_pool WHERE exam_id=?',
                                        (exam['id'],)).fetchone()[0], 13)

    def test_bank_changed_during_save_rolls_back_whole_update(self):
        self.save()
        before = self.fixture.exam()
        def stale_pool(cursor, eid, pid):
            cursor.execute("UPDATE problem_templates SET status='retired' WHERE id=105")
            return freeze_plan(cursor, eid, g.exam_question_plan, self.enable_paper)
        self.namespace['replace_exam_question_pool'] = stale_pool
        changed = [dict(row) for row in self.raw]
        changed[0]['random_count'] = 3
        self.save(changed)
        self.assertEqual(self.fixture.exam(), before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM exam_question_pool').fetchone()[0], 13)
        self.assertEqual(self.db.execute('SELECT status FROM problem_templates WHERE id=105').fetchone()[0], 'active')

    def test_started_exam_rejects_policy_change_but_accepts_metadata_with_retired_question(self):
        self.save()
        before = self.fixture.exam()['question_plan']
        self.db.execute('INSERT INTO exam_user_questions VALUES(9,7,1,101,1)')
        self.db.execute("UPDATE problem_templates SET status='retired' WHERE id=101")
        self.db.commit()
        self.save([dict(paper_id=20, mandatory_ids=[201], random_count=1)])
        self.assertEqual(self.fixture.exam()['question_plan'], before)
        self.fixture.post_exam(name='metadata edited', question_plan=json.dumps(self.raw))
        self.assertEqual(self.fixture.exam()['name'], 'metadata edited')
        self.assertEqual(self.fixture.exam()['question_plan'], before)
        options = self.fixture.client.get('/admin/exams/question-options?exam_id=9').get_json()
        self.assertTrue(options['locked'])
        self.assertIn(101, [row['id'] for row in options['questions']])
        # A stale form cannot accidentally erase the stored multi-bank policy.
        self.fixture.post_exam(name='stale form')
        self.assertEqual(self.fixture.exam()['name'], 'metadata edited')

    def test_question_options_are_admin_only(self):
        with self.fixture.client.session_transaction() as state:
            state['username'] = 'student'
        self.assertEqual(self.fixture.client.get('/admin/exams/question-options').status_code, 403)

    def test_secondary_bank_cannot_be_closed_while_exam_is_unfinished(self):
        self.save()
        self.db.create_function('NOW', 0, lambda: '2026-10-19 10:00:00')
        cursor = arrangements.Cursor(self.db)
        self.assertTrue(paper_has_unfinished_exam(cursor, 20))
        self.assertFalse(paper_has_unfinished_exam(cursor, 99))
        self.db.execute("UPDATE exam_batches SET end_time='2026-10-19 08:00:00'")
        self.assertFalse(paper_has_unfinished_exam(cursor, 20))

    def test_history_includes_all_source_banks_but_only_the_selected_exam(self):
        self.db.executescript('''
            ALTER TABLE user_responses ADD COLUMN id INT;
            ALTER TABLE user_responses ADD COLUMN template_id INT;
            ALTER TABLE user_responses ADD COLUMN paper_id INT;
            ALTER TABLE user_responses ADD COLUMN problem_text TEXT;
            ALTER TABLE user_responses ADD COLUMN user_answer REAL;
            ALTER TABLE user_responses ADD COLUMN correct_answer REAL;
            ALTER TABLE user_responses ADD COLUMN is_correct INT;
            ALTER TABLE user_responses ADD COLUMN attempt_count INT;
            ALTER TABLE user_responses ADD COLUMN response_time TEXT;
            ALTER TABLE user_responses ADD COLUMN time_taken REAL;
            INSERT INTO user_responses(exam_id,user_id,id,template_id,paper_id,user_answer,correct_answer,is_correct,attempt_count,time_taken)
                VALUES(9,25,1,101,10,1,1,1,1,5), (9,25,2,201,20,2,2,1,1,6),
                      (99,25,3,202,20,3,3,1,1,7);
        ''')
        fixture = exam_routes.SebEntryRouteTests()
        fixture.setUp()
        fixture.login_student()
        with fixture.client.session_transaction() as state:
            state['selected_exam_id'] = 9
            state['selected_exam_batch_id'] = 7
        ns = fixture.app.view_functions['exam.history'].__wrapped__.__globals__
        ns.update(get_db_connection=lambda: arrangements.Connection(self.db),
                  render_template=lambda template, **data: data)
        response = fixture.client.get('/history')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual({row['template_id'] for row in data['responses']}, {101, 201})
        self.assertEqual(data['stats']['total'], 2)

    def assignment_driver(self, before_lock=None):
        events = []
        db = self.db

        class TrackedCursor(arrangements.Cursor):
            def execute(self, sql, params=()):
                events.append(sql)
                if 'FOR UPDATE' in sql and before_lock:
                    before_lock()
                return super().execute(sql, params)

            def executemany(self, sql, params):
                events.append(sql)
                return super().executemany(sql, params)

        class TrackedConnection(arrangements.Connection):
            def cursor(self, **kwargs):
                return TrackedCursor(db)

            def commit(self):
                events.append('commit')
                super().commit()

            def rollback(self):
                events.append('rollback')
                super().rollback()

            def close(self):
                events.append('close')

        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'get_user_exam_problem_display_info')
        ns = dict(app=self.fixture.app, get_exam_by_id=self.fixture.deps['get_exam_by_id'],
                  get_db_connection=lambda: TrackedConnection(db), json=json,
                  select_plan_questions=select_plan_questions, select_exam_template_ids=select_exam_template_ids,
                  choice_definition=choice_definition, get_exam_metadata_cache=Mock(return_value=None),
                  set_exam_metadata_cache=lambda *args: events.append('cache'))
        exec(compile(ast.Module(body=[func], type_ignores=[]), 'app.py', 'exec'), ns)
        return ns[func.name], events

    def test_first_entry_persists_cross_bank_questions_and_reentry_reuses_them(self):
        self.save()
        choice = dict(mode='choice_v1', question_type='single_choice',options=[dict(id=1,text='甲'),dict(id=2,text='乙')],correct_ids=[1])
        self.db.execute('UPDATE problem_templates SET generation_strategy=? WHERE paper_id=10',(json.dumps(choice),))
        self.db.commit()
        assign, events = self.assignment_driver()
        first = assign(1, 9)
        self.assertEqual(sum('FOR UPDATE' in e for e in events), 1)
        self.assertLess(events.index('commit'), events.index('close'))
        self.assertLess(events.index('close'), events.index('cache'))
        self.assertEqual(len(first), 8)
        self.assertTrue(all(first[tid]['is_choice'] for tid in list(first)[:3]))
        self.assertTrue(all(not first[tid]['is_choice'] for tid in list(first)[3:]))
        self.assertTrue({101, 201, 202}.issubset(first))
        self.assertEqual(sum(q['paper_id'] == 20 for q in first.values()), 5)
        self.assertEqual(sorted(q['display_number'] for q in first.values()), list(range(1, 9)))
        self.db.execute("UPDATE problem_templates SET status='retired' WHERE id=101")
        self.db.execute("INSERT INTO problem_templates(id,paper_id,template_name,status) VALUES(999,10,'new version','active')")
        self.db.commit()
        self.fixture.app.secret_key = 'changed-secret'
        self.assertEqual(assign(1, 9), first)
        self.db.execute('INSERT OR IGNORE INTO exam_batch_students VALUES(9,7,2,1)')
        self.db.commit()
        other = assign(2, 9)
        self.assertEqual(len(other), 8)
        self.assertTrue({101, 201, 202}.issubset(other))
        self.assertNotIn(999, other)
        self.assertNotEqual(list(first), list(other))

    def test_saved_assignment_and_progress_reads_do_not_lock_or_redraw(self):
        self.save()
        assign, events = self.assignment_driver()
        first = assign(1, 9)
        # An unrelated change to the pool must not force a saved student to redraw.
        self.db.execute('DELETE FROM exam_question_pool')
        self.db.commit()
        for persist in (True, False):
            events.clear()
            self.assertEqual(assign(1, 9, persist=persist), first)
            self.assertFalse(any('FOR UPDATE' in e or 'exam_question_pool' in e or 'INSERT' in e for e in events))
            self.assertIn('rollback', events)

    def test_preview_never_creates_assignment_and_matches_formal_entry(self):
        self.save()
        assign, events = self.assignment_driver()
        preview = assign(1, 9, persist=False)
        self.assertEqual(len(preview), 8)
        self.assertFalse(any('FOR UPDATE' in e or 'INSERT' in e or e == 'commit' for e in events))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM exam_user_questions').fetchone()[0], 0)
        self.assertEqual(assign(1, 9), preview)

    def test_legacy_preview_without_frozen_pool_is_read_only(self):
        self.db.execute('UPDATE exams SET question_plan=NULL, question_count=3 WHERE id=9')
        self.db.commit()
        assign, events = self.assignment_driver()
        preview = assign(1, 9, persist=False)
        self.assertEqual(len(preview), 3)
        self.assertFalse(any('FOR UPDATE' in e or 'INSERT' in e for e in events))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM exam_question_pool').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM exam_user_questions').fetchone()[0], 0)
        self.assertEqual(assign(1, 9), preview)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM exam_question_pool').fetchone()[0], 5)

    def test_concurrent_first_entry_rechecks_assignment_after_lock(self):
        self.save()

        def other_request_finishes():
            self.db.execute('INSERT INTO exam_user_questions VALUES(9,7,1,201,1)')
            self.db.commit()

        assign, events = self.assignment_driver(before_lock=other_request_finishes)
        result = assign(1, 9)
        self.assertEqual(list(result), [201])
        self.assertFalse(any('INSERT' in e for e in events))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM exam_user_questions').fetchone()[0], 1)

    def test_assignment_reloads_admin_policy_after_waiting_for_lock(self):
        self.save()

        def admin_changes_policy():
            self.db.execute('UPDATE exams SET question_plan=NULL, question_count=1 WHERE id=9')
            self.db.commit()

        assign, events = self.assignment_driver(before_lock=admin_changes_policy)
        result = assign(1, 9)
        self.assertEqual(len(result), 1)
        self.assertTrue(all(info['paper_id'] == 10 for info in result.values()))

    def test_assignment_failure_explicitly_rolls_back_and_does_not_cache(self):
        self.save()
        self.db.execute('DELETE FROM exam_question_pool')
        self.db.commit()
        assign, events = self.assignment_driver()
        with self.assertRaisesRegex(ValueError, '考试固定题目缺失'):
            assign(1, 9)
        self.assertNotIn('commit', events)
        self.assertNotIn('cache', events)
        self.assertEqual(events[-2:], ['rollback', 'close'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM exam_user_questions').fetchone()[0], 0)

    def test_removed_student_is_not_assigned_after_waiting_for_lock(self):
        self.save()

        def admin_removes_student():
            self.db.execute('UPDATE exam_batch_students SET is_active=0 WHERE exam_id=9 AND user_id=1')
            self.db.commit()

        assign, events = self.assignment_driver(before_lock=admin_removes_student)
        self.assertEqual(assign(1, 9), {})
        self.assertFalse(any('INSERT' in e for e in events))
        self.assertEqual(events[-2:], ['rollback', 'close'])


if __name__ == '__main__':
    unittest.main()
