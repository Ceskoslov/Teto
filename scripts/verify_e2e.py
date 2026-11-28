"""Exercise a real HTTP server and compare its reports with CLI outputs."""

import argparse
import json
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SECRET = "E2E_PRIVATE_BODY_5d3c"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def http(base_url, body=None, content_type="application/json"):
    headers = {"Content-Type": content_type} if body is not None else {}
    request = Request(base_url, data=body, headers=headers)
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def wait_ready(base_url):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            status, _ = http(base_url + "/openapi.json")
            if status == 200:
                return
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.1)
    raise RuntimeError("HTTP server did not become ready within 45 seconds")


@contextmanager
def server(mode, image, *, env=None):
    if mode == "docker":
        subprocess.run(["docker", "info"], check=True, capture_output=True)
        subprocess.run(["docker", "build", "--tag", image, "."], cwd=ROOT, check=True)
        result = subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--publish",
                "127.0.0.1::8000",
                image,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        container = result.stdout.strip()
        try:
            port = (
                subprocess.run(
                    ["docker", "port", container, "8000/tcp"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                .stdout.strip()
                .rsplit(":", 1)[1]
            )
            url = f"http://127.0.0.1:{port}"
            wait_ready(url)
            yield url
            logs = subprocess.run(
                ["docker", "logs", container],
                capture_output=True,
                text=True,
                check=True,
            )
            require(
                SECRET not in logs.stdout + logs.stderr,
                "Container logs exposed request contents",
            )
        finally:
            subprocess.run(
                ["docker", "rm", "--force", container], check=False, capture_output=True
            )
    else:
        # Passing an already-bound socket avoids port-selection races.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "evaluator.api:app",
                    "--fd",
                    str(listener.fileno()),
                    "--no-access-log",
                ],
                cwd=ROOT,
                env=env,
                pass_fds=(listener.fileno(),),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                url = f"http://127.0.0.1:{port}"
                wait_ready(url)
                yield url
            finally:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                require(
                    SECRET not in stdout + stderr,
                    "Server logs exposed request contents",
                )


def write_json(path, value):
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def verify(base_url, output_dir):
    fixture = json.loads((ROOT / "examples/input.json").read_text(encoding="utf-8"))
    results = []
    for decision in ("hold", "release"):
        data = json.loads(json.dumps(fixture))
        if decision == "release":
            for case in data["cases"]:
                case["baseline"]["output"] = " ".join(case["expected_terms"])
                case["candidate"] = dict(case["baseline"])
        source = output_dir / f"{decision}.input.json"
        cli_path = output_dir / f"{decision}.cli.json"
        write_json(source, data)
        cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "evaluator",
                "evaluate",
                "--input",
                str(source),
                "--output",
                str(cli_path),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        require(
            cli.returncode == (1 if decision == "hold" else 0),
            "Unexpected CLI exit code",
        )
        body = source.read_bytes()
        status, report = http(base_url + "/evaluate", body)
        require(status == 200, "Valid evaluation must return HTTP 200")
        require(
            report == json.loads(cli_path.read_text(encoding="utf-8")),
            "CLI/API reports differ",
        )
        require(
            report["recommendation"]["decision"] == decision,
            "Unexpected release recommendation",
        )
        if decision == "hold":
            expected = json.loads(
                (ROOT / "examples/input.expected.json").read_text(encoding="utf-8")
            )
            require(
                report["gates"] == expected["gates"],
                "Fixture gates differ from independent oracle",
            )
            require(
                report["recommendation"] == expected["recommendation"],
                "Fixture recommendation differs from oracle",
            )
        repeated_status, repeated = http(base_url + "/evaluate", body)
        require(
            repeated_status == 200 and repeated == report,
            "Repeated requests must be deterministic",
        )
        write_json(output_dir / f"{decision}.api.json", report)
        results.append(
            {
                "decision": decision,
                "http_status": status,
                "cli_exit_code": cli.returncode,
                "reports_equal": True,
            }
        )

    invalid = json.loads(json.dumps(fixture))
    invalid["cases"][0]["prompt"] = SECRET
    invalid["cases"][0]["candidate"]["cost_usd"] = SECRET
    checks = [
        (b'{"private":', "application/json", 400),
        (b'{"x": 1, "x": 2}', "application/json", 400),
        (b'{"value": NaN}', "application/json", 400),
        (b"\xff", "application/json", 400),
        (b"{}", "text/plain", 415),
        (json.dumps(invalid).encode(), "application/json", 422),
    ]
    for body, media_type, expected_status in checks:
        status, error = http(base_url + "/evaluate", body, media_type)
        require(status == expected_status, "Unexpected invalid-request HTTP status")
        require(
            "error" in error and SECRET not in json.dumps(error),
            "Error envelope exposed request contents",
        )
    return {"evaluations": results, "invalid_request_checks": len(checks)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("local", "docker"), required=True)
    parser.add_argument("--image", default="llm-release-eval:local")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/e2e")
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # A failed rerun must not leave a stale success summary behind.
    summary_path = output_dir / "summary.json"
    summary_path.unlink(missing_ok=True)
    try:
        with server(args.mode, args.image) as base_url:
            summary = verify(base_url, output_dir)
        if args.mode == "docker":
            summary["image_id"] = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", args.image],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        write_json(summary_path, {"mode": args.mode, "passed": True, **summary})
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
        print(
            "End-to-end verification failed; check Docker availability and server startup.",
            file=sys.stderr,
        )
        return 1
    print(f"End-to-end verification passed ({args.mode}); evidence: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
