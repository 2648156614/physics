import copy
import json
import unittest
from unittest.mock import Mock, patch

from ai_question_example import example
from services import ai_question_agent as agent
from services.ai_template_compiler import CompileError


def transport_plan():
    plan = example()
    strategy = plan['template']['generation_strategy']
    strategy['hidden_vars'] = [{'name':'q','spec':{'type':'choice','values':[],'range':[0.5,5]}}]
    strategy['derived_vars'] = [{'name':name,'expression':expr} for name,expr in strategy['derived_vars'].items()]
    strategy['key_vars'] = [{'name':'T','spec':{'type':'choice','values':[1,2,3,4],'range':[]}}]
    strategy['free_vars'] = []
    strategy['target_answers'] = [{'index':0,'spec':dict(strategy.pop('target_answer'),range=[])}]
    strategy['parameter_cases'] = []
    strategy['solve_for'] = ['q']
    plan['analysis']['variable_units'] = [{'name':name,'unit':unit} for name,unit in plan['analysis']['variable_units'].items()]
    plan.update(status='ready_for_validation',reason='适合当前系统')
    return plan


class AgentTests(unittest.TestCase):
    def test_strict_schema_rejects_missing_extra_and_wrong_type(self):
        for change in ('extra','missing','bool'):
            plan = transport_plan()
            if change == 'extra': plan['template']['valid_cases'] = []
            if change == 'missing': del plan['analysis']['derivation']
            if change == 'bool': plan['template']['answer_count'] = True
            with self.assertRaises(CompileError): agent.normalize_plan(plan)

    def test_transport_normalizes_maps_without_losing_dependencies(self):
        plan = agent.normalize_plan(transport_plan())
        self.assertEqual(plan['template']['generation_strategy']['derived_vars']['k'],'3*q')
        self.assertEqual(plan['template']['generation_strategy']['target_answers']['0']['values'], list(range(5,51,5)))

    def test_program_rejection_is_fed_back_for_bounded_repair(self):
        plan = agent.normalize_plan(transport_plan()); progress = Mock()
        with patch.object(agent,'call_model',return_value=plan) as model, patch.object(agent,'compile_isolated',side_effect=[CompileError('参数不合格'),{'report':{}}]) as compiler:
            result = agent.generate({'text':'题目'}, '规范', [], progress)
            self.assertEqual(result['status'],'ready'); self.assertEqual(model.call_count,2)
            self.assertEqual(compiler.call_count,2)
            self.assertIn('参数不合格',json.dumps(model.call_args.args[0],ensure_ascii=False))

    def test_three_failed_plans_stop_instead_of_falling_back(self):
        with patch.object(agent,'call_model',side_effect=lambda _: agent.normalize_plan(transport_plan())) as model, patch.object(agent,'compile_isolated',side_effect=CompileError('不合格')):
            result = agent.generate({},'规范',[],Mock())
            self.assertEqual(result['status'],'failed'); self.assertEqual(model.call_count,3)
            self.assertIsNone(result['artifact'])

    def test_unsuitable_question_is_not_sent_to_compiler(self):
        plan = transport_plan(); plan.update(status='unsuitable',template=None,reason='需要作图判分')
        with patch.object(agent,'call_model',return_value=agent.normalize_plan(plan)), patch.object(agent,'compile_isolated') as compiler:
            result = agent.generate({},'规范',[],Mock())
            self.assertEqual(result['status'],'unsuitable'); compiler.assert_not_called()

    def test_api_request_uses_strict_schema_and_no_storage(self):
        plan = transport_plan()
        response = Mock()
        response.read.return_value = json.dumps({'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(plan)}]}]}).encode()
        context = Mock(); context.__enter__ = Mock(return_value=response); context.__exit__ = Mock(return_value=False)
        opener = Mock(); opener.open.return_value = context
        with patch.dict('os.environ',{'AI_API_KEY':'test-only','AI_MODEL':'test-model'}), patch('urllib.request.build_opener',return_value=opener):
            result = agent.call_model([{'type':'input_text','text':'题目'}])
            payload = json.loads(opener.open.call_args.args[0].data)
            self.assertFalse(payload['store']); self.assertTrue(payload['text']['format']['strict'])
            self.assertEqual(result['status'],'ready_for_validation')


if __name__ == '__main__': unittest.main()
