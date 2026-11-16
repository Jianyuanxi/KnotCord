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
    # worker 会扫 Codex 会话目录：指到不存在的临时位置，测试里不读真实的 ~/.codex
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))
    # 项目地图默认读 ~/CLAUDE.md：家目录指到不存在的临时位置，测试里读不到真实的地图
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake_home"))
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))
    return home


@pytest.fixture
def conn(trace_home):
    from knotcord.core import db
    c = db.connect()
    yield c
    c.close()
