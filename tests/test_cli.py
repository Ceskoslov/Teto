import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from evaluator import evaluate
from evaluator.cli import main
from evaluator.validation import validate_request

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/input.json"


class CLITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.source = self.directory / "input.json"
        self.output = self.directory / "report.json"
        self.source.write_bytes(FIXTURE.read_bytes())

    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "evaluator", *map(str, arguments)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def evaluate_cli(self, output=None):
        return self.run_cli(
            "evaluate",
            "--input",
            self.source,
            "--output",
            output if output is not None else self.output,
        )

    def test_hold_writes_same_report_as_core(self):
        result = self.evaluate_cli()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        expected = evaluate(validate_request(self.source.read_text()))
        self.assertEqual(json.loads(self.output.read_text()), expected)

    def test_release_exit_code(self):
        data = json.loads(self.source.read_text())
        for case in data["cases"]:
            case["baseline"]["output"] = " ".join(case["expected_terms"])
            case["candidate"] = dict(case["baseline"])
        self.source.write_text(json.dumps(data))
        result = self.evaluate_cli()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            json.loads(self.output.read_text())["recommendation"]["decision"], "release"
        )

    def test_invalid_input_preserves_existing_report_and_hides_values(self):
        secret = "PRIVATE_PROMPT_OUTPUT_1c93"
        data = json.loads(self.source.read_text())
        data["cases"][0]["candidate"]["cost_usd"] = secret
        self.source.write_text(json.dumps(data))
        self.output.write_text("previous report")
        result = self.evaluate_cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.output.read_text(), "previous report")
        self.assertEqual(result.stdout, "")
        self.assertNotIn(secret, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "INVALID_INPUT")

    def test_malformed_and_duplicate_key_json(self):
        for text in ('{"private":', '{"private": 1, "private": 2}'):
            with self.subTest(text=text):
                self.source.write_text(text)
                result = self.evaluate_cli()
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    json.loads(result.stderr)["error"]["code"], "INVALID_JSON"
                )
                self.assertFalse(self.output.exists())

    def test_invalid_utf8(self):
        self.source.write_bytes(b"\xff")
        result = self.evaluate_cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stderr)["error"]["issues"][0]["code"], "UTF8_REQUIRED"
        )

    def test_missing_input(self):
        self.source.unlink()
        result = self.evaluate_cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "IO_ERROR")
        self.assertFalse(self.output.exists())

    def test_input_cannot_be_overwritten_through_aliases(self):
        symlink = self.directory / "symlink.json"
        hardlink = self.directory / "hardlink.json"
        symlink.symlink_to(self.source)
        os.link(self.source, hardlink)
        before = self.source.read_bytes()
        for path in (
            self.source,
            self.directory / "child" / ".." / "input.json",
            symlink,
            hardlink,
        ):
            with self.subTest(path=path):
                result = self.evaluate_cli(path)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    json.loads(result.stderr)["error"]["issues"][0]["code"],
                    "INPUT_OUTPUT_MUST_DIFFER",
                )
                self.assertEqual(self.source.read_bytes(), before)

    def test_missing_output_directory(self):
        result = self.evaluate_cli(self.directory / "missing" / "report.json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stderr)["error"]["issues"][0]["code"],
            "REPORT_WRITE_FAILED",
        )
        self.assertEqual(list(self.directory.glob(".evaluation-*.tmp")), [])

    def test_success_replaces_report_without_leftover_temporary_files(self):
        self.output.write_text("previous report")
        self.assertEqual(self.evaluate_cli().returncode, 1)
        self.assertIsInstance(json.loads(self.output.read_text()), dict)
        self.assertEqual(list(self.directory.glob(".evaluation-*.tmp")), [])

    def test_failed_replace_keeps_previous_report_and_cleans_up(self):
        self.output.write_text("previous report")
        with (
            patch("evaluator.cli.os.replace", side_effect=OSError("private detail")),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = main(
                [
                    "evaluate",
                    "--input",
                    str(self.source),
                    "--output",
                    str(self.output),
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(self.output.read_text(), "previous report")
        self.assertEqual(list(self.directory.glob(".evaluation-*.tmp")), [])
        self.assertNotIn("private detail", stderr.getvalue())

    def test_unexpected_evaluation_failure_is_sanitized(self):
        with (
            patch("evaluator.cli.evaluate", side_effect=RuntimeError("private output")),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = main(
                [
                    "evaluate",
                    "--input",
                    str(self.source),
                    "--output",
                    str(self.output),
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(stderr.getvalue())["error"]["code"], "INTERNAL_ERROR"
        )
        self.assertNotIn("private output", stderr.getvalue())
        self.assertFalse(self.output.exists())

    def test_usage_errors_and_help(self):
        for arguments in ([], ["evaluate"], ["PRIVATE_ARGUMENT"]):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("PRIVATE_ARGUMENT", result.stderr)
                self.assertEqual(
                    json.loads(result.stderr)["error"]["code"], "INVALID_ARGUMENTS"
                )
        result = self.run_cli("evaluate", "--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--input", result.stdout)
        self.assertIn("--output", result.stdout)


if __name__ == "__main__":
    unittest.main()
