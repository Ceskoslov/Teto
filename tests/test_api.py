import io
import json
import logging
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from evaluator import evaluate
from evaluator.api import app
from evaluator.validation import InputValidationError, validate_request

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/input.json"


class APITests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def post(self, text, content_type="application/json"):
        return self.client.post(
            "/evaluate",
            content=text,
            headers={"Content-Type": content_type},
        )

    def test_hold_returns_200_and_core_report(self):
        text = FIXTURE.read_text(encoding="utf-8")
        response = self.post(text)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.json(), evaluate(validate_request(text)))
        self.assertEqual(response.json()["recommendation"]["decision"], "hold")

    def test_cli_api_agreement_for_release_and_hold(self):
        for decision in ("release", "hold"):
            with self.subTest(decision=decision):
                data = json.loads(FIXTURE.read_text())
                if decision == "release":
                    for case in data["cases"]:
                        case["baseline"]["output"] = " ".join(case["expected_terms"])
                        case["candidate"] = dict(case["baseline"])
                text = json.dumps(data)
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "input.json"
                    output = Path(directory) / "report.json"
                    source.write_text(text, encoding="utf-8")
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "evaluator",
                            "evaluate",
                            "--input",
                            str(source),
                            "--output",
                            str(output),
                        ],
                        cwd=ROOT,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    response = self.post(text)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        result.returncode, 0 if decision == "release" else 1
                    )
                    self.assertEqual(response.json(), json.loads(output.read_text()))
                    self.assertEqual(
                        response.json()["recommendation"]["decision"], decision
                    )

    def test_malformed_json_and_duplicate_keys_return_400(self):
        for text in (
            "",
            '{"private":',
            '{"private": 1, "private": 2}',
            '{"nested": {"x": 1, "x": 2}}',
        ):
            with self.subTest(text=text):
                response = self.post(text)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "INVALID_JSON")

    def test_nonfinite_json_constants_return_400(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                response = self.post('{"value": ' + constant + "}")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()["error"]["issues"][0]["code"],
                    "NONFINITE_JSON_NUMBER",
                )

    def test_invalid_utf8_returns_400(self):
        response = self.post(b"\xff")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["issues"][0]["code"], "UTF8_REQUIRED")

    def test_unsupported_or_missing_content_type_returns_415(self):
        for media_type in ("text/plain", "application/xml", "application/jsonp"):
            with self.subTest(media_type=media_type):
                self.assertEqual(self.post("{}", media_type).status_code, 415)
        response = self.client.post("/evaluate", content=b"{}")
        self.assertEqual(response.status_code, 415)

    def test_json_content_type_parameters_are_accepted(self):
        response = self.post(FIXTURE.read_bytes(), "Application/JSON; charset=utf-8")
        self.assertEqual(response.status_code, 200)

    def test_model_validation_returns_422_and_shared_error_envelope(self):
        data = json.loads(FIXTURE.read_text())
        data["cases"][0]["candidate"]["cost_usd"] = -1
        text = json.dumps(data)
        with self.assertRaises(InputValidationError) as caught:
            validate_request(text)
        response = self.post(text)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), caught.exception.to_dict())

    def test_valid_json_with_wrong_shape_returns_422(self):
        for text in ("[]", "null", '"text"', "{}"):
            with self.subTest(text=text):
                self.assertEqual(self.post(text).status_code, 422)

    def test_request_policy_override_is_rejected(self):
        data = json.loads(FIXTURE.read_text())
        data["policy"] = {"id": "client-controlled"}
        response = self.post(json.dumps(data))
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("client-controlled", response.text)

    def test_body_and_exception_details_are_not_in_responses_or_logs(self):
        secret = "PRIVATE_BODY_AND_EXCEPTION_7a43"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        data = json.loads(FIXTURE.read_text())
        data["cases"][0]["prompt"] = secret
        data["cases"][0]["candidate"]["output"] = secret
        data["cases"][0]["candidate"]["cost_usd"] = secret
        response = self.post(json.dumps(data))
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        with patch("evaluator.api.evaluate", side_effect=RuntimeError(secret)):
            response = self.post(FIXTURE.read_bytes())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn(secret, response.text)
        self.assertNotIn("Traceback", response.text)
        self.assertNotIn(secret, stream.getvalue())

    def test_repeated_requests_are_deterministic(self):
        first = self.post(FIXTURE.read_bytes())
        second = self.post(FIXTURE.read_bytes())
        self.assertEqual(first.content, second.content)

    def test_routing_errors_have_sanitized_envelopes(self):
        response = self.client.get("/evaluate")
        self.assertEqual(response.status_code, 405)
        self.assertIn("POST", response.headers["allow"])
        self.assertEqual(
            response.json()["error"]["issues"][0]["code"], "METHOD_NOT_ALLOWED"
        )
        response = self.client.get("/PRIVATE_PATH_93bd")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("PRIVATE_PATH_93bd", response.text)

    def test_openapi_documents_request_and_error_statuses(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        operation = response.json()["paths"]["/evaluate"]["post"]
        self.assertIn("application/json", operation["requestBody"]["content"])
        self.assertTrue(
            {"200", "400", "415", "422", "500"} <= operation["responses"].keys()
        )


if __name__ == "__main__":
    unittest.main()
