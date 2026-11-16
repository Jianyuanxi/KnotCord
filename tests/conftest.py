"""测试公共设置：KNOTCORD_HOME 指到临时目录，绝不碰真实的 ~/.knotcord、~/.claude-mem。"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def trace_home(tmp_path, monkeypatch):
    home = tmp_path / "trace_home"
    monkeypatch.setenv("KNOTCORD_HOME", str(home))
    monkeypatch.delenv("KNOTCORD_INTERNAL", raising=False)
    return home


@pytest.fixture
def conn(trace_home):
    from knotcord.core import db
    c = db.connect()
    yield c
    c.close()
