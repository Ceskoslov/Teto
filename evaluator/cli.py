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
        description="Compare agent runs or generated responses for a release gate.",
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

    for subcommand in (command, importer, comparison):
        subcommand.add_argument(
            "--output", type=Path, required=True, help="Destination JSON file."
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
    try:
        if any(_same_file(source, args.output) for source in sources):
            print(
                _error("INVALID_ARGUMENTS", "INPUT_OUTPUT_MUST_DIFFER"), file=sys.stderr
            )
            return 2
        inputs = [source.read_text(encoding="utf-8") for source in sources]
    except UnicodeError:
        print(_error("INVALID_INPUT", "UTF8_REQUIRED"), file=sys.stderr)
        return 2
    except (OSError, RuntimeError):
        print(_error("IO_ERROR", "INPUT_READ_FAILED"), file=sys.stderr)
        return 2

    try:
        match args.command:
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
    except (ArithmeticError, RuntimeError, TypeError, ValueError):
        # CLI boundary: never expose evidence or a traceback in diagnostics.
        print(_error("INTERNAL_ERROR", "EVALUATION_FAILED"), file=sys.stderr)
        return 2

    try:
        _atomic_write(args.output, contents)
    except (OSError, UnicodeError):
        print(_error("IO_ERROR", "REPORT_WRITE_FAILED"), file=sys.stderr)
        return 2

    if args.command == "import-nausicaa":
        return 0
    return 0 if result["recommendation"]["decision"] == "release" else 1


def teto_main() -> int:
    return main(prog="teto")
