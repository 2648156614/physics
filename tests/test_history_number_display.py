from datetime import datetime
from pathlib import Path
import unittest
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, DictLoader, select_autoescape


class HistoryNumberDisplayTests(unittest.TestCase):
    def setUp(self):
        self.env = Environment(loader=ChoiceLoader([
            DictLoader({'base.html':'{% block content %}{% endblock %}'}),
            FileSystemLoader(Path(__file__).parents[1]/'templates')]),autoescape=select_autoescape(['html']))
        self.env.globals.update(get_flashed_messages=lambda **kwargs:[],url_for=lambda *args,**kwargs:'/')
        self.format = self.env.get_template('macros/number_display.html').module.answer_number

    def render_history(self,user,correct):
        response = dict(id=1565,template_id=13,template_name='fixture',problem_text='fixture',
                        user_answer=user,correct_answer=correct,is_correct=(user==correct),
                        time_taken=5.6,response_time=datetime(2026,11,19,20,4,6),attempt_count=1)
        return self.env.get_template('history.html').render(responses=[response],stats=None)

    def test_tiny_values_do_not_display_as_zero(self):
        self.assertEqual(str(self.format(6e-7)),'6.00 × 10<sup>-7</sup>')
        self.assertEqual(str(self.format(-1.602e-19)),'-1.60 × 10<sup>-19</sup>')
        self.assertEqual(str(self.format(5e-324)),'4.94 × 10<sup>-324</sup>')

    def test_integer_zero_and_rounding_at_exponent_boundary(self):
        self.assertEqual(str(self.format(398491237)),'398491237')
        self.assertEqual(str(self.format(0)),'0')
        self.assertEqual(str(self.format(.99999)),'1.00 × 10<sup>0</sup>')

    def test_screenshot_record_renders_nonzero_and_nonnegative_relative_error(self):
        html=self.render_history(111,6e-7)
        self.assertIn('6.00 × 10<sup>-7</sup>',html)
        self.assertIn('相对误差:',html)
        self.assertNotIn('准确度:',html)
        self.assertIn('1.85 × 10<sup>10</sup>%',html)

    def test_true_zero_has_explicit_relative_error_handling(self):
        self.assertIn('不适用（正确答案为 0）',self.render_history(1,0))
        self.assertIn('0%</span>',self.render_history(0,0))


if __name__=='__main__':
    unittest.main()
