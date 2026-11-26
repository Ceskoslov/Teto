"""File-based adapter for the shared evaluator."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

from .agent_runs import compare_runs, import_nausicaa, parse_agent_run
from .core import evaluate
from .runner import run_suite
from .scoring import METRICS, score_artifact
from .suite_scoring import SUITE_METRICS, score_suite_run
from .suites import read_suite
from .validation import InputValidationError, validate_request


def _error(code: str, issue_code: str) -> str:
    return json.dumps(
        {
            "error": {
                "code": code,
                "issues": [{"path": "$", "code": issue_code}],
            }
        }
    )


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's default error can echo arbitrary command-line values.
        self.exit(2, _error("INVALID_ARGUMENTS", "CHECK_COMMAND_USAGE") + "\n")


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog=prog,
        description="Run and score agent suites or compare saved runs.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser(
        "evaluate",
        help="Evaluate a JSON input file.",
        allow_abbrev=False,
    )
    command.add_argument(
        "--input", type=Path, required=True, help="UTF-8 JSON input file."
    )

    importer = commands.add_parser(
        "import-nausicaa",
        help="Convert an offline Nausicaa summary to Agent Run v1.",
        allow_abbrev=False,
    )
    importer.add_argument(
        "--input", type=Path, required=True, help="Nausicaa summary.json."
    )
    importer.add_argument("--mode", choices=("core_only", "task"), required=True)
    importer.add_argument("--artifact-id", required=True, help="Version or build ID.")

    comparison = commands.add_parser(
        "compare",
        help="Compare two Agent Run v1 artifacts.",
        allow_abbrev=False,
    )
    comparison.add_argument(
        "--baseline", type=Path, required=True, help="Baseline JSON file."
    )
    comparison.add_argument(
        "--candidate", type=Path, required=True, help="Candidate JSON file."
    )

    runner = commands.add_parser(
        "run", help="Execute a pinned suite.", allow_abbrev=False
    )
    runner.add_argument("--input", type=Path, required=True, help="Suite JSON file.")
    runner.add_argument("--harness", type=Path, required=True, help="Pinned Python script.")
    runner.add_argument("--artifact-id", required=True, help="Version or build ID.")

    for subcommand in (command, importer, comparison, runner):
        subcommand.add_argument(
            "--output", type=Path, required=True, help="Destination JSON file."
        )

    scoring = commands.add_parser(
        "score", help="Score one saved artifact.", allow_abbrev=False
    )
    formats = scoring.add_subparsers(dest="format", required=True)
    for name, metrics in {**METRICS, "suite-run": SUITE_METRICS}.items():
        source = formats.add_parser(name, allow_abbrev=False)
        source.add_argument(
            "--input", type=Path, required=True, help="Evidence JSON file."
        )
        source.add_argument(
            "--output", type=Path, required=True, help="Score report file."
        )
        source.add_argument(
            "--metric",
            action="append",
            choices=metrics,
            help="Select a metric. Repeat to select several.",
        )
        if name in {"nausicaa", "ohmu"}:
            source.add_argument(
                "--artifact-id", required=True, help="Version or build ID."
            )
        if name == "suite-run":
            source.add_argument(
                "--suite", type=Path, required=True, help="Original Suite v1 JSON."
            )
        if name == "nausicaa":
            source.add_argument("--mode", choices=("core_only", "task"), required=True)
        if name == "ohmu":
            source.add_argument(
                "--cases",
                type=Path,
                required=True,
                help="Original Ohmu case annotations.",
            )
    return parser


def _same_file(source: Path, destination: Path) -> bool:
    # resolve handles relative paths and symlinks; samefile handles hard links.
    return source.resolve() == destination.resolve() or (
        destination.exists() and source.samefile(destination)
    )


def _atomic_write(destination: Path, contents: str) -> None:
    temporary = None
    try:
        # The destination's directory keeps rename on the same filesystem.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=".evaluation-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def main(argv: list[str] | None = None, *, prog: str = "python -m evaluator") -> int:
    args = _parser(prog).parse_args(argv)
    sources = (
        (args.baseline, args.candidate) if args.command == "compare" else (args.input,)
    )
    if args.command == "score" and args.format == "ohmu":
        sources += (args.cases,)
    if args.command == "score" and args.format == "suite-run":
        sources += (args.suite,)
    if args.command == "run":
        sources += (args.harness,)
    try:
        if any(_same_file(source, args.output) for source in sources):
            print(
                _error("INVALID_ARGUMENTS", "INPUT_OUTPUT_MUST_DIFFER"), file=sys.stderr
            )
            return 2
        if args.command == "run":
            suite_text = args.input.read_bytes().decode("utf-8")
            harness = args.harness.read_bytes()
        elif args.command == "score":
            # Preserve the original newlines for evidence and annotation digests.
            inputs = [source.read_bytes().decode("utf-8") for source in sources]
        else:
            inputs = [source.read_text(encoding="utf-8") for source in sources]
    except UnicodeError:
        print(_error("INVALID_INPUT", "UTF8_REQUIRED"), file=sys.stderr)
        return 2
    except (OSError, RuntimeError):
        print(_error("IO_ERROR", "INPUT_READ_FAILED"), file=sys.stderr)
        return 2

    try:
        match args.command:
            case "run":
                run = run_suite(read_suite(suite_text), harness, args.artifact_id)
                result = run.model_dump(mode="json")
            case "import-nausicaa":
                run = import_nausicaa(inputs[0], args.mode, args.artifact_id)
                result = run.model_dump(mode="json")
            case "compare":
                baseline, candidate = inputs
                result = compare_runs(
                    parse_agent_run(baseline, "$.baseline"),
                    parse_agent_run(candidate, "$.candidate"),
                )
            case "evaluate":
                result = evaluate(validate_request(inputs[0]))
            case "score":
                if args.format == "suite-run":
                    result = score_suite_run(*inputs, metrics=args.metric)
                else:
                    result = score_artifact(
                        inputs[0],
                        source=args.format,
                        metrics=args.metric,
                        artifact_id=getattr(args, "artifact_id", None),
                        mode=getattr(args, "mode", None),
                        annotations_text=inputs[1] if len(inputs) > 1 else None,
                    )
        contents = (
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        )
    except InputValidationError as exception:
        print(json.dumps(exception.to_dict()), file=sys.stderr)
        return 2
    except OSError:
        print(_error("IO_ERROR", "EVALUATION_IO_FAILED"), file=sys.stderr)
        return 2
    except (ArithmeticError, RuntimeError, TypeError, ValueError):
        # CLI boundary: never expose evidence or a traceback in diagnostics.
        print(_error("INTERNAL_ERROR", "EVALUATION_FAILED"), file=sys.stderr)
        return 2

    try:
        _atomic_write(args.output, contents)
    except (OSError, UnicodeError):
        print(_error("IO_ERROR", "REPORT_WRITE_FAILED"), file=sys.stderr)
        return 2

    if args.command in {"import-nausicaa", "run", "score"}:
        return 0
    return 0 if result["recommendation"]["decision"] == "release" else 1


def teto_main() -> int:
    return main(prog="teto")
