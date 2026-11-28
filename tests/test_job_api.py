import json
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from evaluator.api import http_error_handler
from evaluator.job_api import job_router
from evaluator.job_store import JobStore
from evaluator.worker import process_next

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
AUTH = {"Authorization": "Bearer test-token"}


class JobsAPITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "jobs.sqlite"
        app = FastAPI(exception_handlers={HTTPException: http_error_handler})
        app.include_router(job_router(self.db, "test-token", self.root))
        self.client = TestClient(app, headers=AUTH)
        self.addCleanup(self.client.close)

    def post(self, data, key="example"):
        return self.client.post("/v1/jobs", json=data, headers={"Idempotency-Key": key})

    def process(self):
        store = JobStore(self.db)
        with store.worker():
            self.assertTrue(process_next(store))
            self.assertFalse(process_next(store))

    @staticmethod
    def ohmu():
        return {
            "schema_version": 1,
            "kind": "import",
            "evidence": {
                "format": "ohmu",
                "artifact_id": "example-ohmu",
                "input": (EXAMPLES / "ohmu-evaluation.json")
                .read_bytes()
                .decode("utf-8"),
                "cases": (EXAMPLES / "ohmu-cases.json").read_bytes().decode("utf-8"),
            },
        }

    def make_bundle(self):
        bundle = self.root / "scripted"
        bundle.mkdir()
        for name, source in (
            ("suite.json", "scripted-suite.json"),
            ("policy.json", "scripted-policy.json"),
            ("harness.py", "scripted-harness.py"),
        ):
            shutil.copyfile(EXAMPLES / source, bundle / name)
        return bundle, {
            "schema_version": 1,
            "kind": "run",
            "bundle": "scripted",
            "artifact_id": "worker-smoke",
        }

    def test_ohmu_import_preserves_bytes_and_matches_cli(self):
        data = self.ohmu()
        submission = self.post(data)
        self.assertEqual(submission.status_code, 202)
        url = submission.headers["location"]
        self.assertEqual(self.client.get(url).json()["status"], "queued")
        self.assertEqual(self.client.get(url + "/reports/score.json").status_code, 404)
        self.assertEqual(
            self.post(dict(reversed(list(data.items())))).json(), submission.json()
        )
        self.assertEqual(self.post(data | {"schema_version": 2}).status_code, 422)
        changed = data | {"evidence": data["evidence"] | {"artifact_id": "changed"}}
        self.assertEqual(self.post(changed).status_code, 409)

        self.process()
        response = self.client.get(url)
        self.assertEqual(response.json()["status"], "succeeded")
        self.assertEqual(response.headers["cache-control"], "no-store")
        for name, source in (("input.json", "input"), ("cases.json", "cases")):
            self.assertEqual(
                self.client.get(url + f"/evidence/{name}").content,
                data["evidence"][source].encode("utf-8"),
            )
        output = self.root / "cli-report.json"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "evaluator",
                "score",
                "ohmu",
                "--input",
                str(EXAMPLES / "ohmu-evaluation.json"),
                "--cases",
                str(EXAMPLES / "ohmu-cases.json"),
                "--artifact-id",
                "example-ohmu",
                "--output",
                str(output),
            ],
            cwd=ROOT,
            check=True,
            timeout=10,
        )
        self.assertEqual(
            self.client.get(url + "/reports/score.json").content, output.read_bytes()
        )

    @unittest.skipUnless(
        (sys.platform, platform.machine(), platform.python_version())
        == ("linux", "x86_64", "3.13.12"),
        "requires the example suite environment pin",
    )
    def test_launch_uses_snapshot_and_import_can_hold_with_the_same_policy(self):
        bundle, data = self.make_bundle()
        response = self.post(data)
        self.assertEqual(response.status_code, 202)
        url = response.headers["location"]
        shutil.rmtree(bundle)
        self.assertEqual(self.post(data).json(), response.json())
        self.assertEqual(self.client.get(url + "/evidence/harness.py").status_code, 404)

        self.process()
        self.assertEqual(self.client.get(url).json()["status"], "succeeded")
        run = self.client.get(url + "/evidence/run.json").json()
        self.assertEqual(run["artifact_id"], data["artifact_id"])
        gate = self.client.get(url + "/reports/gate.json").json()
        self.assertEqual(gate["recommendation"]["decision"], "release")

        run["trials"][0]["output_sha256"] = "0" * 64
        response = self.post(
            {
                "schema_version": 1,
                "kind": "import",
                "evidence": {
                    "format": "suite-run",
                    "input": json.dumps(run),
                    "suite": self.client.get(url + "/evidence/suite.json").text,
                    "policy": self.client.get(url + "/evidence/policy.json").text,
                },
            },
            key="rescore",
        )
        self.assertEqual(response.status_code, 202)
        imported_url = response.headers["location"]
        self.process()
        self.assertEqual(self.client.get(imported_url).json()["status"], "succeeded")
        gate = self.client.get(imported_url + "/reports/gate.json").json()
        self.assertEqual(gate["recommendation"]["decision"], "hold")

    def test_bad_policy_fails_before_launch(self):
        bundle, data = self.make_bundle()
        (bundle / "policy.json").write_text("{}", encoding="utf-8")
        url = self.post(data).headers["location"]
        with patch("evaluator.worker.run_suite") as runner:
            self.process()
            runner.assert_not_called()
        status = self.client.get(url).json()
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"]["issues"][0]["code"], "INVALID_SUITE_POLICY")

    def test_failed_import_retains_input_and_sanitizes_error(self):
        data = self.ohmu()
        data["evidence"]["input"] = '{"secret": private input}'
        url = self.post(data).headers["location"]
        self.process()
        status = self.client.get(url)
        self.assertEqual(status.json()["status"], "failed")
        self.assertEqual(status.json()["error"]["code"], "INVALID_JSON")
        self.assertNotIn("private input", status.text)
        self.assertEqual(
            self.client.get(url + "/evidence/input.json").text,
            data["evidence"]["input"],
        )

    def test_authentication_and_submission_boundaries(self):
        data = self.ohmu()
        with TestClient(self.client.app) as unauthenticated:
            self.assertEqual(
                unauthenticated.post("/v1/jobs", json=data).status_code, 401
            )
            result = unauthenticated.get("/v1/jobs/missing/evidence/input.json")
            self.assertEqual(result.status_code, 401)
            self.assertEqual(result.headers["www-authenticate"], "Bearer")
        self.assertEqual(self.post(data, key="").status_code, 400)
        headers = {"Idempotency-Key": "example", "Content-Type": "application/json"}
        duplicated = self.client.post(
            "/v1/jobs", content='{"kind": 1, "kind": 2}', headers=headers
        )
        self.assertEqual(duplicated.status_code, 400)
        with patch("evaluator.job_api.MAX_JOB_BYTES", 8):
            self.assertEqual(self.post(data).status_code, 413)
        _, launch = self.make_bundle()
        self.assertEqual(self.post(launch | {"bundle": "../scripted"}).status_code, 422)
        self.assertEqual(self.post(launch | {"bundle": "missing"}).status_code, 404)
        self.assertEqual(self.client.get("/v1/jobs/missing").status_code, 404)
