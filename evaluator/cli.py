"""File-based adapter for the shared evaluator."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

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


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="python -m evaluator",
        description="Compare generated responses and write a release report.",
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
    command.add_argument(
        "--output", type=Path, required=True, help="Destination JSON report file."
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if _same_file(args.input, args.output):
            print(
                _error("INVALID_ARGUMENTS", "INPUT_OUTPUT_MUST_DIFFER"), file=sys.stderr
            )
            return 2
        text = args.input.read_text(encoding="utf-8")
    except UnicodeError:
        print(_error("INVALID_INPUT", "UTF8_REQUIRED"), file=sys.stderr)
        return 2
    except (OSError, RuntimeError):
        print(_error("IO_ERROR", "INPUT_READ_FAILED"), file=sys.stderr)
        return 2

    try:
        report = evaluate(validate_request(text))
        contents = (
            json.dumps(
                report,
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

    return 0 if report["recommendation"]["decision"] == "release" else 1
