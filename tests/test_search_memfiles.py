"""内置记忆文件索引测试。"""

import os

from knotcord.search import api, memfiles


def write(root, proj, name, text):
    d = root / proj / "memory"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text(text, encoding="utf-8")
    return f


def test_project_from_dir():
    assert memfiles.project_from_dir("E--Span") == "Span"
    assert memfiles.project_from_dir("C--Users-alice") == "alice"
    assert memfiles.project_from_dir("D--alice-desktop-----") == "desktop"
    assert memfiles.project_from_dir("-----") == "-----"


def test_reindex_basic_frontmatter_and_skip_index(conn, tmp_path):
    root = tmp_path / "projects"
    write(root, "E--Span", "a.md", "---\nname: bt-panel\ndescription: 宝塔面板的反代说明\ntype: reference\n---\n正文：nginx 配置要点\n")
    write(root, "E--Span", "MEMORY.md", "# 索引\n- [x](a.md)\n")
    write(root, "E--Road", "b.md", "没有 frontmatter 的第一行\n第二行 路由规则\n")
    assert memfiles.reindex(conn, root=str(root)) == 2
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM memfiles")}
    assert set(rows) == {"bt-panel", "b"}
    assert rows["bt-panel"]["description"] == "宝塔面板的反代说明" and rows["bt-panel"]["project"] == "Span"
    assert rows["b"]["description"] == "没有 frontmatter 的第一行" and rows["b"]["project"] == "Road"
    # 正文也进了索引
    out = api.search(conn, "nginx 配置要点")
    assert "memfile bt-panel：宝塔面板的反代说明" in out
    assert api.search(conn, "路由规则").startswith(f"m{rows['b']['id']} Road memfile b")


def test_reindex_incremental(conn, tmp_path):
    root = tmp_path / "projects"
    f = write(root, "E--Span", "a.md", "---\nname: a\ndescription: 旧描述\n---\n旧正文 鸡蛋\n")
    g = write(root, "E--Span", "g.md", "---\nname: g\ndescription: 其他\n---\n其他\n")
    assert memfiles.reindex(conn, root=str(root)) == 2
    assert memfiles.reindex(conn, root=str(root)) == 0  # mtime 没变：跳过
    # 改内容和 mtime
    f.write_text("---\nname: a\ndescription: 新描述\n---\n新正文 苹果\n", encoding="utf-8")
    st = f.stat()
    os.utime(f, (st.st_atime, st.st_mtime + 10))
    assert memfiles.reindex(conn, root=str(root)) == 1
    assert conn.execute("SELECT description FROM memfiles WHERE name='a'").fetchone()[0] == "新描述"
    assert api.search(conn, "苹果") != "无结果"
    assert api.search(conn, "鸡蛋") == "无结果"
    assert conn.execute("SELECT count(*) FROM memfiles").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM docs WHERE src='memfile'").fetchone()[0] == 2
    # 删文件：memfiles 行和 doc 都没了
    g.unlink()
    assert memfiles.reindex(conn, root=str(root)) == 0
    assert conn.execute("SELECT count(*) FROM memfiles").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM docs WHERE src='memfile'").fetchone()[0] == 1
    assert api.search(conn, "其他") == "无结果"


def test_reindex_missing_root(conn, tmp_path):
    assert memfiles.reindex(conn, root=str(tmp_path / "nope")) == 0


def test_reindex_does_not_touch_files(conn, tmp_path):
    root = tmp_path / "projects"
    f = write(root, "E--Span", "a.md", "---\nname: a\ndescription: d\n---\nx\n")
    before = (f.read_bytes(), f.stat().st_mtime_ns)
    memfiles.reindex(conn, root=str(root))
    assert (f.read_bytes(), f.stat().st_mtime_ns) == before
