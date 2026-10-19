import unittest

from services.submission_receipt_service import receipt_key, answer_fingerprint, restore_receipt_state


class SubmissionReceiptServiceTests(unittest.TestCase):
    def test_receipt_keys_are_scoped_and_reject_invalid_identifiers(self):
        self.assertNotEqual(receipt_key(1,'a'*32),receipt_key(2,'a'*32))
        for value in ['../anything','a'*31,None,123]:
            with self.assertRaises(ValueError):
                receipt_key(1,value)

    def test_receipt_binds_answers_exam_and_question(self):
        original=answer_fingerprint(dict(answer=4,time_taken=10),9,2)
        self.assertEqual(original,answer_fingerprint(dict(answer=4,time_taken=20),9,2))
        for payload,exam,question in [(dict(answer=8),9,2),(dict(answer=4),10,2),(dict(answer=4),9,3)]:
            self.assertNotEqual(original,answer_fingerprint(payload,exam,question))

    def test_lost_response_restores_result_session_only_for_original_variant(self):
        original=dict(exam_id=9,paper_id=10,display_number=2,user_id=1,token='old')
        result=dict(original,token='new',total_attempts=1)
        receipt=dict(source_token='old',current_problem=result)
        state=dict(current_problem=original)
        restore_receipt_state(receipt,state)
        self.assertEqual(state['current_problem'],result)
        for overrides in [dict(exam_id=11),dict(display_number=3),dict(user_id=2),dict(token='another-variant')]:
            current=dict(original,**overrides)
            state=dict(current_problem=current)
            restore_receipt_state(receipt,state)
            self.assertEqual(state['current_problem'],current)


if __name__=='__main__':
    unittest.main()
