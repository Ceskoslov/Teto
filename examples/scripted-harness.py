"""Offline protocol example. Produce a fixture result without a provider."""

import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
source = Path("input.txt").read_text(encoding="utf-8")
Path("result.txt").write_text(source.upper(), encoding="utf-8")
json.dump(
    {
        "schema_version": 1,
        "reported_success": True,
        "cost_microusd": 0,
        "tool_calls": {"read": 1, "write": 1},
    },
    sys.stdout,
)
