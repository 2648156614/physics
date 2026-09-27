import hashlib
import unittest

from services.seb_exam_service import (
    build_finish_summary,
    calculate_config_key_request_hash,
    normalize_config_key,
    parse_checkbox,
    verify_config_key_request,
)


class SebExamServiceTests(unittest.TestCase):
    def test_config_key_request_hash_uses_url_then_key_and_removes_fragment(self):
        key = 'a' * 64
        expected = hashlib.sha256(f'https://exam.test/problem_ajax/1{key}'.encode('utf-8')).hexdigest()
        self.assertEqual(
            calculate_config_key_request_hash('https://exam.test/problem_ajax/1#answer', key),
            expected,
        )

    def test_request_hash_comparison_accepts_uppercase_and_rejects_wrong_hash(self):
        key = 'b' * 64
        url = 'https://exam.test/api/submit/1'
        value = calculate_config_key_request_hash(url, key)
        self.assertTrue(verify_config_key_request(url, key, value.upper()))
        self.assertFalse(verify_config_key_request(url, key, '0' * 64))

    def test_invalid_config_keys_are_rejected(self):
        self.assertIsNone(normalize_config_key('short'))
        self.assertIsNone(normalize_config_key('z' * 64))

    def test_checkbox_and_finish_summary(self):
        self.assertTrue(parse_checkbox('on'))
        self.assertFalse(parse_checkbox(None))
        self.assertEqual(
            build_finish_summary(5, 8),
            {
                'completed_count': 5,
                'incomplete_count': 3,
                'total_count': 8,
                'all_completed': False,
            },
        )


if __name__ == '__main__':
    unittest.main()
