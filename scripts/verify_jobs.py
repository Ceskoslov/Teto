"""Exercise the Jobs API and worker over loopback with saved producer evidence."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

from verify_e2e import ROOT, require, server

EXAMPLES = ROOT / "examples"
TOKEN = "local-verification-token"


def request(url, payload=None, key=None):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
    with urlopen(Request(url, data=body, headers=headers), timeout=10) as response:
        return response.status, response.read()


def read_text(path):
    return path.read_bytes().decode("utf-8")


def run_cli(output, *args):
    subprocess.run(
        [sys.executable, "-m", "evaluator", *args, "--output", str(output)],
        cwd=ROOT,
        check=True,
        timeout=30,
    )
    return output.read_bytes()


def verify_job(base_url, env, key, job):
    payload = {"schema_version": 1, **job}
    status, body = request(base_url + "/v1/jobs", payload, key)
    require(status == 202, "Job submission must return 202")
    require(
        request(base_url + "/v1/jobs", payload, key) == (status, body),
        "Idempotent replay returned a different job",
    )
    url = base_url + json.loads(body)["status_url"]
    require(json.loads(request(url)[1])["status"] == "queued", "Job is not queued")

    subprocess.run(
        [sys.executable, "-m", "evaluator.worker", "--once"],
        cwd=ROOT,
        env=env,
        check=True,
        timeout=30,
    )
    final = json.loads(request(url)[1])
    require(final["status"] == "succeeded", f"Job {key} failed: {final['error']}")
    return url


def verify_imports(base_url, env, root, summary):
    for name, input_path, fields, cli_args in (
        ("nausicaa", summary, {"mode": "task"}, ("--mode", "task")),
        (
            "ohmu",
            EXAMPLES / "ohmu-evaluation.json",
            {"cases": read_text(EXAMPLES / "ohmu-cases.json")},
            ("--cases", str(EXAMPLES / "ohmu-cases.json")),
        ),
    ):
        url = verify_job(
            base_url,
            env,
            name,
            {
                "kind": "import",
                "evidence": {
                    "format": name,
                    "input": read_text(input_path),
                    "artifact_id": f"{name}-smoke",
                    **fields,
                },
            },
        )
        require(
            request(url + "/evidence/input.json")[1] == input_path.read_bytes(),
            f"{name} evidence changed during submission",
        )
        expected = run_cli(
            root / f"{name}-score.json",
            "score",
            name,
            "--input",
            str(input_path),
            "--artifact-id",
            f"{name}-smoke",
            *cli_args,
        )
        require(
            request(url + "/reports/score.json")[1] == expected,
            f"{name} report differs",
        )


def verify_launch(base_url, env, root, bundle):
    url = verify_job(
        base_url,
        env,
        "launch",
        {
            "kind": "run",
            "bundle": bundle.name,
            "artifact_id": "http-smoke",
        },
    )
    run = root / "run.json"
    run.write_bytes(request(url + "/evidence/run.json")[1])
    args = ("--input", str(run), "--suite", str(bundle / "suite.json"))
    expected_score = run_cli(root / "score.json", "score", "suite-run", *args)
    expected_gate = run_cli(
        root / "gate.json",
        "gate",
        *args,
        "--policy",
        str(bundle / "policy.json"),
    )
    require(
        request(url + "/reports/score.json")[1] == expected_score, "Suite score differs"
    )
    require(
        request(url + "/reports/gate.json")[1] == expected_gate, "Policy report differs"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--nausicaa", type=Path, required=True, help="Saved task suite summary.json"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="teto-http-jobs-") as directory:
        root = Path(directory)
        bundle = root / "bundles" / "scripted"
        bundle.mkdir(parents=True)
        for name, source in (
            ("suite.json", "scripted-suite.json"),
            ("harness.py", "scripted-harness.py"),
            ("policy.json", "scripted-policy.json"),
        ):
            shutil.copyfile(EXAMPLES / source, bundle / name)
        worker_env = os.environ | {"TETO_JOB_DB": str(root / "jobs.sqlite")}
        worker_env.pop("TETO_JOB_TOKEN", None)
        server_env = worker_env | {
            "TETO_JOB_TOKEN": TOKEN,
            "TETO_JOB_BUNDLES": str(bundle.parent),
        }
        with server("local", None, env=server_env) as base_url:
            verify_imports(base_url, worker_env, root, args.nausicaa.resolve())
            verify_launch(base_url, worker_env, root, bundle)
    print(
        "HTTP jobs and CLI produced identical reports for Nausicaa, Ohmu, and the scripted suite."
    )


if __name__ == "__main__":
    main()
