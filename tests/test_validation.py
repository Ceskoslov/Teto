import json
import unittest
from pathlib import Path

from evaluator.validation import (
    InputValidationError,
    validate_request,
)

ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / "examples" / "input.json"


def load_fixture() -> dict:
    return json.loads(INPUT_PATH.read_text(encoding="utf-8"))


class ValidationTests(unittest.TestCase):
    def assert_invalid(self, text: str) -> InputValidationError:
        with self.assertRaises(InputValidationError) as caught:
            validate_request(text)

        return caught.exception

    def test_fixture_is_valid(self):
        request = validate_request(INPUT_PATH.read_text(encoding="utf-8"))

        self.assertEqual(request.product, "support-assistant")
        self.assertEqual(len(request.cases), 4)

    def test_invalid_numbers(self):
        for field in ("latency_ms", "cost_usd"):
            for value in (-1, "800", True, None):
                with self.subTest(field=field, value=value):
                    data = load_fixture()
                    data["cases"][0]["candidate"][field] = value

                    self.assert_invalid(json.dumps(data))

    def test_missing_candidate(self):
        data = load_fixture()
        del data["cases"][0]["candidate"]

        error = self.assert_invalid(json.dumps(data))

        self.assertIn(
            {
                "path": "cases[0].candidate",
                "code": "REQUIRED_FIELD",
            },
            error.to_dict()["error"]["issues"],
        )

    def test_invalid_json(self):
        examples = [
            '{"product":',
            '{"product": "a", "product": "b"}',
            '{"value": NaN}',
            '{"value": Infinity}',
            '{"value": -Infinity}',
        ]

        for text in examples:
            with self.subTest(text=text):
                error = self.assert_invalid(text)
                self.assertEqual(error.code, "INVALID_JSON")

    def test_lone_surrogates_are_invalid_json(self):
        for value in ("\ud800", "\udfff"):
            with self.subTest(value=repr(value)):
                data = load_fixture()
                data["cases"][0]["candidate"]["output"] = value
                error = self.assert_invalid(json.dumps(data))
                self.assertEqual(error.code, "INVALID_JSON")

    def test_overflowing_number(self):
        data = load_fixture()
        data["cases"][0]["candidate"]["cost_usd"] = 123456789

        text = json.dumps(data).replace("123456789", "1e999")
        error = self.assert_invalid(text)

        self.assertIn(
            {
                "path": "cases[0].candidate.cost_usd",
                "code": "FINITE_NUMBER_REQUIRED",
            },
            error.to_dict()["error"]["issues"],
        )

    def test_empty_output_is_valid(self):
        data = load_fixture()
        data["cases"][0]["candidate"]["output"] = ""

        request = validate_request(json.dumps(data))

        self.assertEqual(request.cases[0].candidate.output, "")

    def test_case_relationships(self):
        for condition in (
            "duplicate_case_id",
            "same_artifact",
            "no_quality_annotations",
            "empty_cases",
        ):
            with self.subTest(condition=condition):
                data = load_fixture()

                if condition == "duplicate_case_id":
                    data["cases"][1]["id"] = data["cases"][0]["id"]
                elif condition == "same_artifact":
                    data["candidate_artifact_id"] = data["baseline_artifact_id"]
                elif condition == "no_quality_annotations":
                    for case in data["cases"]:
                        case["expected_terms"] = []
                else:
                    data["cases"] = []

                self.assert_invalid(json.dumps(data))

    def test_relationship_error_codes(self):
        for condition, expected_code in (
            ("duplicate_case_id", "DUPLICATE_CASE_ID"),
            ("same_artifact", "ARTIFACT_IDS_MUST_DIFFER"),
            ("no_quality_annotations", "QUALITY_ANNOTATION_REQUIRED"),
        ):
            with self.subTest(condition=condition):
                data = load_fixture()
                if condition == "duplicate_case_id":
                    data["cases"][1]["id"] = data["cases"][0]["id"]
                elif condition == "same_artifact":
                    data["candidate_artifact_id"] = data["baseline_artifact_id"]
                else:
                    for case in data["cases"]:
                        case["expected_terms"] = []

                error = self.assert_invalid(json.dumps(data))
                self.assertIn(
                    expected_code,
                    [issue.code for issue in error.issues],
                )

    def test_blank_and_padded_identifiers(self):
        for field in (
            "product",
            "dataset_version",
            "baseline_artifact_id",
            "candidate_artifact_id",
            "id",
        ):
            for value in ("", "   ", " padded", "padded "):
                with self.subTest(field=field, value=value):
                    data = load_fixture()
                    target = data["cases"][0] if field == "id" else data
                    target[field] = value
                    self.assert_invalid(json.dumps(data))

    def test_blank_prompt(self):
        for value in ("", " \t\n"):
            with self.subTest(value=value):
                data = load_fixture()
                data["cases"][0]["prompt"] = value
                error = self.assert_invalid(json.dumps(data))
                self.assertIn("BLANK_PROMPT", [issue.code for issue in error.issues])

    def test_annotations_are_preserved(self):
        data = load_fixture()
        terms = [" Ｒｅｓｅｔ   LINK ", " Email "]
        data["cases"][0]["expected_terms"] = terms
        request = validate_request(json.dumps(data))
        self.assertEqual(request.cases[0].expected_terms, terms)

    def test_unicode_normalized_annotation_duplicates(self):
        data = load_fixture()
        data["cases"][0]["forbidden_terms"] = ["SUPPORT", "ＳＵＰＰＯＲＴ"]
        error = self.assert_invalid(json.dumps(data))
        self.assertIn("DUPLICATE_TERM", [issue.code for issue in error.issues])

    def test_invalid_annotations(self):
        for expected, forbidden in (
            (["   "], []),
            (["Reset Link", " reset   link "], []),
            (["support"], [" SUPPORT "]),
        ):
            with self.subTest(expected=expected, forbidden=forbidden):
                data = load_fixture()
                data["cases"][0]["expected_terms"] = expected
                data["cases"][0]["forbidden_terms"] = forbidden

                self.assert_invalid(json.dumps(data))

    def test_empty_annotations_on_one_case_are_valid(self):
        data = load_fixture()
        data["cases"][0]["expected_terms"] = []
        data["cases"][0]["forbidden_terms"] = []

        validate_request(json.dumps(data))

    def test_errors_do_not_expose_input(self):
        secret = "SENSITIVE_CONTENT_123"
        data = load_fixture()

        data["cases"][0]["candidate"]["cost_usd"] = secret
        data["cases"][0]["prompt"] = secret
        data["cases"][0]["candidate"]["output"] = secret
        data[secret] = secret

        error = self.assert_invalid(json.dumps(data))
        serialized = json.dumps(error.to_dict())

        self.assertNotIn(secret, serialized)
        self.assertNotIn(secret, str(error))

    def test_numeric_representation_is_preserved(self):
        data = load_fixture()
        data["cases"][0]["baseline"]["latency_ms"] = 800
        data["cases"][0]["candidate"]["latency_ms"] = 800.0

        request = validate_request(json.dumps(data))

        self.assertIs(
            type(request.cases[0].baseline.latency_ms),
            int,
        )
        self.assertIs(
            type(request.cases[0].candidate.latency_ms),
            float,
        )


if __name__ == "__main__":
    unittest.main()
