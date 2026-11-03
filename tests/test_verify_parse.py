import unittest

import helpers  # noqa: F401 - sets sys.path

from kickroll.verify import extract_report, json_objects, parse_review


class JsonObjectsTest(unittest.TestCase):
    def test_nested_and_braces_in_strings(self):
        text = 'prose {not json} then {"a": {"b": 1}, "s": "x } y"} and {"c": [1, {"d": 2}]}'
        self.assertEqual(json_objects(text), [{"a": {"b": 1}, "s": "x } y"}, {"c": [1, {"d": 2}]}])

    def test_empty(self):
        self.assertEqual(json_objects(""), [])
        self.assertEqual(json_objects(None), [])


class ExtractReportTest(unittest.TestCase):
    def test_summary_with_braces_outside_a_fence(self):
        text = 'Done.\n{"status": "done", "summary": "uses {name} templates", "files_changed": ["a.py"]}'
        self.assertEqual(extract_report(text)["summary"], "uses {name} templates")

    def test_fenced_with_nested_object(self):
        text = '```json\n{"status": "done", "summary": "s", "files_changed": [], "extra": {"k": 1}}\n```'
        self.assertEqual(extract_report(text)["extra"], {"k": 1})

    def test_last_report_wins_and_missing_keys_ignored(self):
        text = ('{"status": "done", "summary": "first", "files_changed": []}\n'
                '{"status": "done", "summary": "second", "files_changed": []}\n{"note": "trailing"}')
        self.assertEqual(extract_report(text)["summary"], "second")
        self.assertIsNone(extract_report('{"status": "done"}'))


class ParseReviewTest(unittest.TestCase):
    def test_reason_with_braces(self):
        text = 'Review:\n{"A1": "pass", "GEN": "pass", "reason": "the {x} placeholder is filled"}'
        self.assertEqual(parse_review(text, None, ["A1", "GEN"]), (True, []))

    def test_structured_wins_and_garbage_fails(self):
        self.assertEqual(parse_review("", {"A1": "fail"}, ["A1"]), (False, ["A1"]))
        self.assertEqual(parse_review("no json here", None, ["A1"]), (False, ["A1"]))


if __name__ == "__main__":
    unittest.main()
