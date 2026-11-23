import json
import io
import openpyxl  # Load real numeric dependencies before legacy isolated service fixtures.
import unittest
from unittest.mock import patch, Mock
from services import choice_exam_service
from jinja2 import Environment, FileSystemLoader

import test_generation_validation as author_tests
from services import question_generation_service as generation
from services.choice_question_service import choice_definition, generate_choice_problem, grade_selection, selection_labels
import test_seb_entry_routes as exam_tests


def definition(kind='multiple_choice'):
    return dict(mode='choice_v1', question_type=kind,
                options=[dict(id=i, text=f'选项{i}', image_filename='') for i in range(1, 5)],
                correct_ids=[1, 3] if kind=='multiple_choice' else [2])


class ChoiceTests(unittest.TestCase):
    def test_whole_question_image_allows_empty_option_contents(self):
        fixture=author_tests.AuthoringValidationTests();fixture.setUp()
        fixture.cursor.lastrowid=28
        fixture.app.view_functions['question.admin_add_problem'].__globals__['save_uploaded_file']=Mock(return_value='whole-question.png')
        response=fixture.client.post('/admin/add_problem',data=dict(template_name='整题图片',problem_text='',
            question_type='single_choice',choice_count='4',choice_correct='3',paper_id='1',
            problem_image=(io.BytesIO(b'fixture'),'whole-question.png')))
        self.assertEqual(response.status_code,302)
        fixture.conn.commit.assert_called_once()
        insert=next(c for c in fixture.cursor.execute.call_args_list if 'INSERT INTO problem_templates' in c.args[0])
        d=json.loads(insert.args[1][-1])
        self.assertTrue(d['options_in_stem'])
        problem=generate_choice_problem(dict(id=1,template_name='整题图片',problem_text=insert.args[1][1]),d,2)
        self.assertEqual([o['id'] for o in problem['choice_options']],[1,2,3,4])
        self.assertTrue(grade_selection(problem,[3])[1])

    def test_empty_options_without_whole_question_image_still_fail(self):
        d=definition();d['options']=[dict(id=i,text='') for i in range(1,5)]
        with self.assertRaises(ValueError):choice_definition(d)
        d['options_in_stem']=True
        with self.assertRaises(ValueError):generate_choice_problem(dict(id=1,template_name='missing image',problem_text='<p> </p>'),d,2)

    def test_whole_text_question_needs_only_correct_selection(self):
        fixture=author_tests.AuthoringValidationTests();fixture.setUp()
        fixture.cursor.lastrowid=28
        response=fixture.client.post('/admin/add_problem',data=dict(template_name='',
            problem_text='哪项正确？\nA. 甲\nB. 乙\nC. 丙\nD. 丁',question_type='multiple_choice',
            choice_count='4',choice_correct=['1','3'],paper_id='1'))
        self.assertEqual(response.status_code,302)
        fixture.conn.commit.assert_called_once()
        insert=next(c for c in fixture.cursor.execute.call_args_list if 'INSERT INTO problem_templates' in c.args[0])
        self.assertTrue(insert.args[1][0])
        d=json.loads(insert.args[1][-1]);self.assertTrue(d['options_in_stem'])
        self.assertEqual(d['correct_ids'],[1,3])

    def problem(self, kind='multiple_choice'):
        return generate_choice_problem(dict(id=200, template_name='选择题', problem_text='<p>选择正确选项</p>'), definition(kind), 2)

    def test_generation_bypasses_all_formula_and_reasonableness_checks(self):
        template=dict(id=200, template_name='选择题', problem_text='题干', variables='missing', solution_formula='undefined', generation_strategy=json.dumps(definition()))
        with patch.object(generation,'get_template',return_value=template), patch.object(generation,'evaluate_solution_formula',side_effect=AssertionError('numeric path')):
            problem=generation.generate_problem_from_template(200)
        self.assertEqual(problem['correct_answers'], [5])
        self.assertNotIn('correct_ids', problem['choice_options'][0])
        self.assertEqual(generation.validate_template_definition('', '', 1, definition()), {})

    def test_exact_grading_and_invalid_submissions(self):
        problem=self.problem()
        self.assertTrue(grade_selection(problem,[3,1])[1])
        for ids in [[1],[1,2,3],[2,4]]:
            self.assertFalse(grade_selection(problem,ids)[1])
        for ids in [None,[],[1,1],[True],['1'],[9],1]:
            with self.subTest(ids=ids), self.assertRaises(ValueError):grade_selection(problem,ids)
        with self.assertRaises(ValueError):grade_selection(self.problem('single_choice'),[1,2])

    def test_definition_validation_and_escaping(self):
        for change in [dict(correct_ids=[]),dict(correct_ids=[9]),dict(options=[]),dict(question_type='invalid')]:
            with self.assertRaises(ValueError):choice_definition(dict(definition(),**change))
        d=definition();d['options'][0]['text']='<script>alert(1)</script>'
        problem=generate_choice_problem(dict(id=1,template_name='test',problem_text='题干'),d,2)
        self.assertNotIn('<script>',problem['problem_text'])
        self.assertEqual(selection_labels(5),'A、C')

    def test_history_formats_choices_and_keeps_numeric_format(self):
        env=Environment(loader=FileSystemLoader('templates'))
        macro=env.get_template('macros/number_display.html').module.answer_number
        self.assertEqual(macro(5,self.problem()['problem_text']).strip(),'A、C')
        self.assertEqual(macro(5).strip(),'5')

    def test_submission_uses_cached_key_and_never_numeric_tolerance(self):
        fixture=exam_tests.SebEntryRouteTests();fixture.setUp()
        ns=fixture.prepare_submission()
        ns['get_problem_by_token'].return_value=self.problem()
        ns['is_correct']=Mock(side_effect=AssertionError('numeric tolerance called'))
        ns['get_db_connection']=Mock()
        with patch.object(choice_exam_service,'save_draft') as saved:
            response=fixture.client.post('/api/submit/2',json=dict(selected_ids=[1,3],answer=255,correct_answers=[255],submission_id='6'*32))
            self.assertEqual(saved.call_args.args[4],[1,3])
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.get_json()['choice_saved'])
        self.assertNotIn('correct',response.get_json())
        self.assertNotIn('correct_answers',response.get_json())
        ns['save_user_response'].assert_not_called()
        ns['is_correct'].assert_not_called()

    def test_wrong_choice_reuses_fixed_question_and_invalid_choice_does_not_save(self):
        for ids, status in [([1],200),([1,1],400),([9],400)]:
            fixture=exam_tests.SebEntryRouteTests();fixture.setUp();ns=fixture.prepare_submission()
            ns['get_problem_by_token'].return_value=self.problem()
            ns['fetch_distinct_problem']=Mock(side_effect=AssertionError('fixed question rerolled'))
            ns['get_db_connection']=Mock()
            with patch.object(choice_exam_service,'save_draft'):
                response=fixture.client.post('/api/submit/2',json=dict(selected_ids=ids,submission_id='5'*32))
            self.assertEqual(response.status_code,status)
            if status==400:ns['save_user_response'].assert_not_called()
            else:self.assertTrue(response.get_json()['choice_saved'])

    def test_authoring_without_formula(self):
        fixture=author_tests.AuthoringValidationTests();fixture.setUp()
        fixture.cursor.lastrowid=28
        response=fixture.client.post('/admin/add_problem',data=dict(template_name='选择题',problem_text='题干',question_type='single_choice',choice_count='2',choice_text_1='甲',choice_text_2='乙',choice_correct='2',paper_id='1'))
        self.assertEqual(response.status_code,302)
        fixture.conn.commit.assert_called_once()
        insert=next(c for c in fixture.cursor.execute.call_args_list if 'INSERT INTO problem_templates' in c.args[0])
        self.assertEqual(json.loads(insert.args[1][-1])['correct_ids'],[2])

    def test_choice_backup_import_preserves_answer_and_remaps_image(self):
        fixture=author_tests.AuthoringValidationTests();fixture.setUp()
        fixture.cursor.fetchone.return_value=None;fixture.cursor.lastrowid=28
        fixture.deps['save_imported_image'].side_effect=lambda image: 'imported.png' if image else None
        d=definition();d['options'][0]['image_filename']='old.png'
        payload=dict(paper=dict(name='fixture'),templates=[dict(template_name='choice',problem_text='题干',variables='',solution_formula='0',answer_count=1,generation_strategy=d,choice_images={'1':dict(filename='old.png',content_base64='fixture')})])
        payload['export_format']='physics_exam_paper_backup'
        response=fixture.client.post('/admin/exam_papers/import',data=dict(paper_file=(io.BytesIO(json.dumps(payload).encode()),'paper.json')))
        self.assertEqual(response.status_code,302)
        fixture.conn.commit.assert_called_once()
        insert=next(c for c in fixture.cursor.execute.call_args_list if 'INSERT INTO problem_templates' in c.args[0])
        restored=json.loads(insert.args[1][-1]);self.assertEqual(restored['correct_ids'],[1,3]);self.assertEqual(restored['options'][0]['image_filename'],'imported.png')

    def test_explicit_hand_in_checks_access_and_does_not_publish_answers(self):
        fixture=exam_tests.SebEntryRouteTests();fixture.setUp();ns=fixture.prepare_submission()
        with fixture.client.session_transaction() as state:state['selected_exam_id']=9
        fixture.deps['get_user_exam_problem_display_info'].return_value[200]['is_choice']=True
        ns['get_db_connection']=Mock();ns['clear_exam_metadata_cache']=Mock()
        with patch.object(choice_exam_service,'lock_answers') as hand_in:
            response=fixture.client.post('/api/exam/hand-in')
            self.assertEqual(response.status_code,200)
            hand_in.assert_called_once_with(ns['get_db_connection'],9,25)
            self.assertNotIn('correct_answers',response.get_json())
            fixture.deps['get_user_exam_access'].return_value={'allowed':False,'status':'unassigned','message':'不在名单中'}
            hand_in.reset_mock()
            self.assertEqual(fixture.client.post('/api/exam/hand-in').status_code,403)
            hand_in.assert_not_called()
