"""Engine code must reach time, randomness, waiting, storage and the network only
through aegisflow.runtime. Otherwise the simulator loses control of it, and a
seed no longer determines a run. This is a static check over the source."""
import ast
from pathlib import Path

import pytest

import aegisflow

PACKAGE = Path(aegisflow.__file__).parent
ENGINE_CODE = ["engine.py", "planner.py", "tools.py", "db.py", "models.py", "demo_data.py"]

BANNED_MODULES = {
    "asyncio", "http", "multiprocessing", "os", "pathlib", "random", "requests", "secrets", "shutil",
    "socket", "subprocess", "tempfile", "threading", "time", "urllib", "uuid",
}
BANNED_CALLS = {
    "datetime.now", "datetime.utcnow", "datetime.today", "date.today", "sqlite3.connect",
}
BANNED_BUILTINS = {"open", "input", "exec", "eval", "id", "hash"}


def violations(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if a.name.split(".")[0] in BANNED_MODULES]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in BANNED_MODULES and node.level == 0:
                found.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_BUILTINS:
                found.append(f"{func.id}()")
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if f"{func.value.id}.{func.attr}" in BANNED_CALLS:
                    found.append(f"{func.value.id}.{func.attr}()")
    return found


@pytest.mark.parametrize("name", [n for n in ENGINE_CODE if (PACKAGE / n).exists()])
def test_engine_code_uses_only_the_seams(name):
    assert violations((PACKAGE / name).read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("source,expected", [
    ("import time", "import time"),
    ("import time as t", "import time"),
    ("from time import sleep", "from time import ..."),
    ("import random", "import random"),
    ("from random import randint", "from random import ..."),
    ("import uuid", "import uuid"),
    ("from datetime import datetime\ndatetime.now()", "datetime.now()"),
    ("import sqlite3\nsqlite3.connect('x')", "sqlite3.connect()"),
    ("open('x')", "open()"),
    ("import socket", "import socket"),
    ("import urllib.request", "import urllib.request"),
])
def test_the_check_catches_each_kind_of_bypass(source, expected):
    assert expected in violations(source)


def test_the_check_allows_the_seam_itself():
    assert violations("from . import runtime\nruntime.current.clock()\nruntime.current.sleep(1)") == []
