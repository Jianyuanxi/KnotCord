"""项目地图：只读用户的地图文件。表格解析、三种声明边及优先级、子目录合并、mtime 触发重建、
检索加分、Codex 开局注入、janitor、CLI 三部分输出。地图文件和项目目录都建在 tmp_path 里。"""

import json
import os
import time

from knotcord import cli, inject, janitor, projmap
from knotcord.core import config, db
from knotcord.core.index import index_doc
from knotcord.search import api

T0 = 1_762_700_000
P = "proj"


# ---------------------------------------------------------------- 造数据的小工具

def cfg(map_file=None, **over):
    data = dict(config.DEFAULTS)
    if map_file is not None:
        data["projmap.map_file"] = str(map_file)
    data.update({k.replace("__", "."): v for k, v in over.items()})
    return config.Config(data)


def use_map(trace_home, map_file):
    """写 config.toml 让 config.load() 指到测试地图（TOML 单引号字符串，反斜杠不转义）。"""
    trace_home.mkdir(parents=True, exist_ok=True)
    (trace_home / "config.toml").write_text(f"[projmap]\nmap_file = '{map_file}'\n", encoding="utf-8")


def mkdir(root, *parts):
    p = root.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def note(conn, project, text, *, files=None, ts=T0, state="active"):
    cur = conn.execute("INSERT INTO notes(project, ts, type, title, body, files, tags, state) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (project, ts, "discovery", text, "", json.dumps(files or [], ensure_ascii=False), "[]", state))
    index_doc(conn, "note", cur.lastrowid, project, ts, text, active=(state == "active"))
    return cur.lastrowid


def session(conn, project, cwd, sid=None):
    conn.execute("INSERT INTO sessions(id, client, project, cwd) VALUES(?,?,?,?)",
                 (sid or f"s-{project}-{time.time_ns()}", "claude", project, str(cwd)))


class World:
    """tmp_path 里的一套假项目：Alpha/Beta/Gamma/Delta/Epsilon 和一份地图。"""

    def __init__(self, root):
        self.root = root
        self.d = {n: mkdir(root, "ws", n) for n in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon")}
        self.map = root / "map" / "CLAUDE.md"

    def write_map(self, extra=""):
        a, b, g, d, e = (self.d[n] for n in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon"))
        write(self.map, f"""# 项目地图

## 自己写的软件
| 名字 | 路径 | 是什么 | 关联 |
|---|---|---|---|
| Alpha（甲） | `{a}` | 电脑端 | 节点和服务器在 Gamma；`{b}\\core\\x.rs` 也给 Beta 用 |
| Beta | `{b}` | 手机端 | 默认值沿用 alpha-core |
| Gamma | `{g}` | 服务器 | — |
| 小工具 Delta | `{d}` | 预览工具 | 同类：Epsilon |
| Epsilon | `{e}` | 复制引擎 | 配套 DeltaX，同类还有 `ui/TabBar.kt` |
{extra}""")


def make_world(tmp_path, extra=""):
    w = World(tmp_path)
    w.write_map(extra)
    return w


def edges(c, project):
    return {x["project"]: (x["kind"], x["why"]) for x in projmap.related(c, project)}


# ---------------------------------------------------------------- 表格解析

MAP_TEXT = r"""# 项目地图

## 自己写的软件
| 名字 | 路径 | 是什么 | 关联 |
|---|---|---|---|
| Alpha（甲） | `E:\Alpha` | Windows 客户端 | 节点在 Gamma |
| Zeta | `D:\Zeta` | 小应用 | — |

## 课程（大三上，`D:\x\desktop\大三上\`）
| 名字 | 路径 | 是什么 | 关联 |
|---|---|---|---|
| 课程甲 | `大三上\课程甲` | 课程目录 | 作业在 `E:\Hw1`、`E:\Hw2` |
| 双路径 | `E:\One`、`E:\Two` | x | — |

- 这是列表 `E:\NotATable`

## 没有基准的小节
| 名字 | 路径 | 是什么 | 关联 |
|---|---|---|---|
| 相对 | `rel\dir` | x | — |
"""


def test_parse_map_rows():
    rows = projmap.parse_map(MAP_TEXT)
    assert [r.name for r in rows] == ["Alpha（甲）", "Zeta", "课程甲", "双路径", "相对"]
    alpha = rows[0]
    assert alpha.path_cell == r"`E:\Alpha`" and alpha.assoc == "节点在 Gamma" and alpha.base is None
    assert rows[2].base == "D:\\x\\desktop\\大三上\\" and rows[2].assoc == r"作业在 `E:\Hw1`、`E:\Hw2`"
    assert rows[1].assoc == "—"


def test_resolve_path_cases():
    r = projmap._resolve_path
    assert r(r"`E:\Alpha`", None) == "E:/Alpha"
    assert r(r"`E:\One`、`E:\Two`", None) == "E:/One"  # 多个反引号路径取第一个
    assert r("—", None) is None and r("", "D:/x") is None
    assert r(r"`大三上\课程甲`", "D:\\x\\desktop\\大三上\\") == "D:/x/desktop/大三上/课程甲"  # 基准以第一段结尾：不重复拼
    assert r(r"`课程\甲`", "D:\\x\\desktop") == "D:/x/desktop/课程/甲"
    assert r(r"`rel\dir`", None) is None  # 相对路径又没有基准：解析不出


def test_load_map_nodes_and_skipped(tmp_path):
    w = make_world(tmp_path)
    desk = mkdir(tmp_path, "desk", "大三上")
    w.write_map(f"""
## 课程（大三上，`{desk}{os.sep}`）
| 名字 | 路径 | 是什么 | 关联 |
|---|---|---|---|
| 课程甲 | `大三上\\课程甲` | 课程目录 | — |
| 副本 | `{w.d['Alpha']}` | 同一个目录写了两行 | — |

## 另一节（标题里没有基准路径）
| 名字 | 路径 | 是什么 | 关联 |
|---|---|---|---|
| 无基准 | `foo\\bar` | x | — |
""")
    m = projmap.load_map(cfg(w.map))
    assert set(m.nodes) == {"Alpha", "Beta", "Gamma", "Delta", "Epsilon", "课程甲"}
    assert m.nodes["Alpha"].name == "Alpha（甲）" and m.nodes["Delta"].name == "小工具 Delta"  # 显示名和键不同
    assert m.nodes["课程甲"].path == str(desk / "课程甲").replace("\\", "/")
    assert any("无基准" in s for s in m.skipped) and any("副本" in s for s in m.skipped)


def test_tokens_and_whole_word():
    assert projmap._tokens("Alpha", "Alpha（甲）") == ("Alpha",)  # 单个汉字不要
    assert projmap._tokens("fooSystem", "守护者 fooSystem") == ("fooSystem", "守护者")
    assert projmap._tokens("AB", "AB 一") == ()  # 太短
    assert projmap._has_word("节点在HUB上", "HUB") and not projmap._has_word("HUBX", "HUB")
    assert not projmap._has_word("hub", "HUB") and not projmap._has_word("xHUB", "HUB")  # 大小写敏感、整词
    assert projmap._has_word("在守护者里", "守护者")


def test_parse_related_section():
    text = r"""# 项目

## 相关项目
- `E:\Beta`：手机版，默认值沿用本仓库；改默认值时想一下 Beta 要不要跟。
- `E:\Gamma`：节点、路由和备份。
  续行也算这一项
- 虚拟机「某某」：验收用。
- `E:\Delta` 没有冒号

全部项目见 `C:\Users\x\CLAUDE.md`。

### 小标题不结束这一节
- `E:\Epsilon`：在子标题下

## 下一节
- `E:\NotMine`：不算
"""
    items = projmap.parse_related(text)
    assert items[0] == ([r"E:\Beta"], "手机版，默认值沿用本仓库；改默认值时想一下 Beta 要不要跟。")  # 路径里的冒号不算
    assert items[1][0] == [r"E:\Gamma"] and items[1][1] == "节点、路由和备份。 续行也算这一项"
    assert items[2] == ([], "验收用。")
    assert items[3] == ([r"E:\Delta"], "相关项目一节")
    assert items[4][0] == [r"E:\Epsilon"] and len(items) == 5
    assert projmap.parse_related("# x\n\n## 别的\n- a\n") is None
    long = projmap.parse_related("## 相关项目\n- `E:\\B`：" + "字" * 100 + "\n")
    assert len(long[0][1]) == projmap.WHY_MAX


# ---------------------------------------------------------------- 三种边、优先级、不补反向边

def test_edge_sources_and_no_reverse(conn, tmp_path):
    w = make_world(tmp_path)
    write(w.d["Beta"] / "CLAUDE.local.md",
          f"## 相关项目\n- `{w.d['Alpha']}\\src`：电脑版，改默认值要想一下。\n- `{tmp_path / 'elsewhere'}`：地图里没有\n")
    res = projmap.build(conn, cfg(w.map))
    # Alpha：b（关联栏绝对路径落在 Beta 下）+ c（整词 Gamma；Beta 也被整词提到，但 b 已经有了，不重复）
    alpha = edges(conn, "Alpha")
    assert list(alpha) == ["Beta", "Gamma"] and alpha["Beta"] == ("declared", "地图关联栏")
    assert alpha["Gamma"] == ("declared", projmap._cut(projmap.load_map(cfg(w.map)).nodes["Alpha"].assoc))
    assert alpha["Gamma"][1].startswith("节点和服务器在 Gamma；")
    # Beta：a（相关项目一节，路径落在 Alpha 目录下）；关联栏里的 alpha-core 小写，不算
    assert edges(conn, "Beta") == {"Alpha": ("declared", "电脑版，改默认值要想一下。")}
    # 不自动补反向边：Gamma 没写，就没有 Gamma→Alpha / Gamma→Beta
    assert "Gamma" not in res and projmap.related(conn, "Gamma") == []
    # c：关联栏提到 Epsilon；DeltaX 不是 Delta（整词）、`ui/TabBar.kt` 是相对路径不算
    assert edges(conn, "Delta") == {"Epsilon": ("declared", "同类：Epsilon")}
    assert projmap.related(conn, "Epsilon") == []
    assert db.kv_get(conn, "projmap_at") and json.loads(db.kv_get(conn, "projmap_files"))


def test_priority_a_over_b_over_c_and_one_per_pair(conn, tmp_path):
    w = make_world(tmp_path)
    # Alpha 对 Beta 同时有 a（相关项目一节）、b（关联栏路径）、c（关联栏里的名字 Beta）：只留 a
    write(w.d["Alpha"] / "CLAUDE.md", f"## 相关项目\n- `{w.d['Beta']}`：手机版\n- `{w.d['Beta']}\\x`：第二条说明不要\n")
    projmap.build(conn, cfg(w.map))
    assert edges(conn, "Alpha")["Beta"] == ("declared", "手机版")
    assert len(projmap.related(conn, "Alpha")) == 2  # Beta、Gamma 各一条


def test_local_md_preferred_but_falls_back_to_claude_md(conn, tmp_path):
    w = make_world(tmp_path)
    write(w.d["Gamma"] / "CLAUDE.local.md", "# 没有相关项目这一节\n")
    write(w.d["Gamma"] / "CLAUDE.md", f"## 相关项目\n- `{w.d['Alpha']}`：客户端\n")
    projmap.build(conn, cfg(w.map))
    assert edges(conn, "Gamma") == {"Alpha": ("declared", "客户端")}
    write(w.d["Gamma"] / "CLAUDE.local.md", f"## 相关项目\n- `{w.d['Beta']}`：另一个\n")  # 本地文件有这一节：只用它
    projmap.build(conn, cfg(w.map))
    assert edges(conn, "Gamma") == {"Beta": ("declared", "另一个")}


def test_self_and_deepest_owner(conn, tmp_path):
    w = make_world(tmp_path)
    inner = mkdir(w.d["Alpha"], "Inner")  # 嵌套在 Alpha 目录里的另一个节点
    write(w.map, w.map.read_text(encoding="utf-8") + f"| Inner | `{inner}` | 嵌套 | — |\n")
    write(w.d["Beta"] / "CLAUDE.md", f"## 相关项目\n- `{inner}\\f.py`：落在最深的 Inner 下\n- `{w.d['Beta']}`：自己指自己\n")
    projmap.build(conn, cfg(w.map))
    assert edges(conn, "Beta") == {"Inner": ("declared", "落在最深的 Inner 下")}


def test_max_caps_declared_edges(conn, tmp_path):
    w = make_world(tmp_path)
    items = "".join(f"- `{w.d[n]}`：{n}\n" for n in ("Beta", "Gamma", "Delta", "Epsilon"))
    write(w.d["Alpha"] / "CLAUDE.md", "## 相关项目\n" + items)
    projmap.build(conn, cfg(w.map, projmap__max=2))
    assert [x["project"] for x in projmap.related(conn, "Alpha")] == ["Beta", "Gamma"]
    assert config.DEFAULTS["projmap.max"] == 6


# ---------------------------------------------------------------- 子目录合并

def test_subdir_projects_merge_both_ways(conn, tmp_path):
    w = make_world(tmp_path)
    sub = mkdir(w.d["Alpha"], "docs", "课程项目")
    session(conn, "课程项目", sub)
    session(conn, "Outsider", mkdir(tmp_path, "elsewhere", "Outsider"))
    note(conn, "课程项目", "x")
    projmap.build(conn, cfg(w.map))
    assert ("课程项目" in edges(conn, "Alpha")) and edges(conn, "Alpha")["课程项目"] == ("subdir", "子目录")
    assert edges(conn, "课程项目") == {"Alpha": ("subdir", "子目录")}
    assert projmap.related(conn, "Outsider") == []


# ---------------------------------------------------------------- 文件缺失、mtime 触发重建

def test_missing_or_empty_map_clears_old(conn, tmp_path):
    w = make_world(tmp_path)
    assert projmap.build(conn, cfg(w.map))
    assert projmap.related(conn, "Alpha")
    w.map.unlink()
    assert projmap.build(conn, cfg(w.map)) == {}  # 不报错
    assert projmap.related(conn, "Alpha") == [] and db.kv_get(conn, "projmap_files") == "[]"
    write(w.map, "# 空地图\n\n没有表格\n")  # 解析不出任何行
    projmap.build(conn, cfg(w.map))
    assert projmap.related(conn, "Alpha") == []


def test_stale_on_map_or_project_file_mtime(conn, tmp_path):
    w = make_world(tmp_path)
    c = cfg(w.map)
    assert projmap.stale(conn, c)  # 没建过
    assert projmap.refresh_if_stale(conn, c) == 2  # 有出边的项目：Alpha、Delta
    assert projmap.refresh_if_stale(conn, c) is None  # 没动：不重建
    future = time.time() + 100
    os.utime(w.map, (future, future))
    assert projmap.stale(conn, c)
    assert projmap.refresh_if_stale(conn, c, now=future + 1) is not None
    assert not projmap.stale(conn, c)
    pf = w.d["Beta"] / "CLAUDE.local.md"  # 新建一个项目说明文件：读到的文件清单变了
    write(pf, f"## 相关项目\n- `{w.d['Gamma']}`：新增\n")
    assert projmap.refresh_if_stale(conn, c, now=future + 2) is not None
    assert "Gamma" in edges(conn, "Beta")
    later = future + 500  # 改它的内容（mtime 晚于上次重建）
    write(pf, f"## 相关项目\n- `{w.d['Delta']}`：改了\n")
    os.utime(pf, (later, later))
    assert projmap.refresh_if_stale(conn, c, now=future + 3) is not None
    assert "Delta" in edges(conn, "Beta") and "Gamma" not in edges(conn, "Beta")
    pf.unlink()  # 删除也会触发
    assert projmap.refresh_if_stale(conn, c, now=later + 10) is not None
    assert projmap.refresh_if_stale(conn, cfg(w.map, projmap__enabled=False)) is None


def test_related_tolerates_missing_or_bad_values(conn):
    assert projmap.related(conn, "nope") == []
    db.kv_set(conn, "projmap:bad", "{不是 json")
    assert projmap.related(conn, "bad") == []
    db.kv_set(conn, "projmap:odd", json.dumps({"a": 1}))
    assert projmap.related(conn, "odd") == []


def test_disabled_does_nothing(conn, tmp_path):
    w = make_world(tmp_path)
    assert projmap.build(conn, cfg(w.map, projmap__enabled=False)) == {}
    assert projmap.related(conn, "Alpha") == []


def test_default_map_file_is_home_claude_md(tmp_path, monkeypatch):
    home = mkdir(tmp_path, "h")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    assert projmap.map_path(cfg()) == home / "CLAUDE.md"
    assert projmap.map_path(cfg(tmp_path / "x.md")) == tmp_path / "x.md"
    assert projmap.load_map(cfg()).nodes == {}  # 假家目录里没有地图


# ---------------------------------------------------------------- 候选统计用的旧逻辑（保留）

def test_excluded_names_and_name_ok():
    bad = ["desktop", "12345", "ab", "scratch-2026-10-09-aaa", ".claude", "New-Chat-2", "D--x--y"]
    assert all(not projmap.name_ok(n, cfg()) for n in bad)
    assert projmap.name_ok("ProjA", cfg()) and projmap.name_ok("图书馆系统", cfg())
    assert not projmap.name_ok("ProjB", cfg(projmap__exclude=["projb"]))


def test_file_pairs_under_other_projects_dir(conn):
    session(conn, "ProjB", "E:/ProjB/src")  # cwd 是子目录：截到项目名那一层
    session(conn, "ProjC", "E:\\ProjB\\sub\\ProjC")  # 嵌套：归最深的
    note(conn, "ProjA", "改了几个文件", files=["E:\\ProjB\\x.py", "e:/projb/x.py", "E:/ProjB/lib/y.py", "rel.py",
                                              "E:/ProjB/sub/ProjC/z.py", "E:/ProjA/own.py"])
    names = {"ProjA", "ProjB", "ProjC"}
    pairs = projmap._file_pairs(conn, names, projmap._project_dirs(conn, names))
    assert pairs == {("ProjA", "ProjB"): 2, ("ProjA", "ProjC"): 1}  # x.py 大小写、斜杠不同算同一个


def test_home_dir_and_appdata_are_not_project_dirs(conn, monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    session(conn, "ProjH", str(home).replace("\\", "/"))
    session(conn, "ProjT", str(home / "AppData" / "Local" / "Temp" / "x").replace("\\", "/"))
    session(conn, "ProjD", "E:/")  # 磁盘根
    session(conn, "ProjK", "E:/ProjK")  # 对照：普通项目目录照常算
    dirs = projmap._project_dirs(conn, {"ProjH", "ProjT", "ProjD", "ProjK"})
    assert dirs == {"e:/projk": {"ProjK"}}


# ---------------------------------------------------------------- 注入：Claude 不输出，Codex 输出

def _set_rel(conn, project, items):
    db.kv_set(conn, projmap.KEY_PREFIX + project, json.dumps(items, ensure_ascii=False))


def _seed_inject(conn):
    conn.execute("INSERT INTO notes(project, ts, type, title, body) VALUES(?,?,?,?,?)",
                 (P, int(time.time()), "decision", "选 sqlite 做存储", "b"))
    return inject.render(conn, P)


def test_inject_related_line_only_for_codex(conn):
    base = _seed_inject(conn)
    _set_rel(conn, P, [{"project": "ProjB", "kind": "declared", "why": "手机版"},
                       {"project": "ProjC", "kind": "subdir", "why": "子目录"}])
    claude = inject.read_for_hook(conn, P)
    assert claude == base and "相关项目" not in claude  # 默认就是 Claude
    assert inject.read_for_hook(conn, P, client="claude") == base
    out = inject.read_for_hook(conn, P, client="codex")
    assert "相关项目：ProjB（手机版）、ProjC（子目录）\n" in out
    assert out.endswith("</knotcord-context>") and out.count("<knotcord-context") == 1


def test_inject_codex_without_related_has_no_line(conn):
    text = _seed_inject(conn)
    assert inject.read_for_hook(conn, P, client="codex") == text
    _set_rel(conn, P, [])
    assert "相关项目" not in inject.read_for_hook(conn, P, client="codex")


def test_inject_codex_related_line_obeys_budget_and_switch(conn, trace_home):
    _set_rel(conn, P, [{"project": "ProjB", "kind": "declared", "why": "手机版"}])
    assert "相关项目：ProjB（手机版）" in inject.read_for_hook(conn, P, client="codex")
    trace_home.mkdir(parents=True, exist_ok=True)
    (trace_home / "config.toml").write_text("[inject]\nbudget_tokens = 5\nhard_chars = 100\n", encoding="utf-8")
    assert "相关项目" not in inject.read_for_hook(conn, P, client="codex")  # 装不下就整行跳过
    (trace_home / "config.toml").write_text("[projmap]\nenabled = false\n", encoding="utf-8")
    assert "相关项目" not in inject.read_for_hook(conn, P, client="codex")


def test_hook_entry_passes_client_to_inject(conn, monkeypatch, capsys):
    from knotcord.hooks import entry
    _seed_inject(conn)
    _set_rel(conn, P, [{"project": "ProjB", "kind": "declared", "why": "手机版"}])
    seen = []
    real = inject.read_for_hook
    monkeypatch.setattr(inject, "read_for_hook", lambda *a, **k: (seen.append(k.get("client")), real(*a, **k))[1])
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", P)
    monkeypatch.setattr(entry, "_CLIENT", "claude")
    entry._session_start({"session_id": "s1", "cwd": P})
    monkeypatch.setattr(entry, "_CLIENT", "codex")
    entry._session_start({"session_id": "s1", "cwd": P})
    assert seen == ["claude", "codex"]


# ---------------------------------------------------------------- 检索加分

def _same_text_docs(conn):
    """三个项目各一条同样的笔记；ts 越新排得越前（bm25 打平，按 ts 倒序）。"""
    for i, proj in enumerate(("Other", "Rel", "Cur")):
        cur = conn.execute("INSERT INTO notes(project, ts, type, title, body, state) VALUES(?,?,?,?,?, 'active')",
                           (proj, T0 + i, "discovery", "蓝色鲸鱼配置", ""))
        index_doc(conn, "note", cur.lastrowid, proj, T0 + i, "蓝色鲸鱼配置")


def _order(conn, cur):
    out = api.search(conn, "蓝色鲸鱼", current_project=cur)
    return [ln.split()[2] for ln in out.splitlines()]  # 行格式：n<id> 日期 项目 ...


def test_declared_related_projects_get_a_small_boost(conn):
    _same_text_docs(conn)
    cfgd = config.load()
    assert float(cfgd.get("search.related_boost")) == 1.1 < float(cfgd.get("search.project_boost"))
    # 没有地图：三条一模一样，按 ts 新旧排（RRF 名次 1/61、1/62、1/63）
    assert _order(conn, "Nobody") == ["Cur", "Rel", "Other"]
    # Other 是声明的相关项目：垫底的 1/63 乘 1.1 反超前两名
    _set_rel(conn, "Nobody", [{"project": "Other", "kind": "declared", "why": "x"}])
    assert _order(conn, "Nobody") == ["Other", "Cur", "Rel"]
    # 旧数据没有 kind 的按 declared 算
    _set_rel(conn, "Nobody", [{"project": "Other", "why": "x"}])
    assert _order(conn, "Nobody") == ["Other", "Cur", "Rel"]


def test_subdir_projects_are_boosted_like_current_project(conn):
    _same_text_docs(conn)  # 名次分：Cur 1/61、Rel 1/62、Other 1/63
    # Other 是子目录（乘 1.2 = 0.01905），Rel 是 declared（乘 1.1 = 0.01774）：Other 在前
    _set_rel(conn, "Nobody", [{"project": "Other", "kind": "subdir", "why": "子目录"},
                              {"project": "Rel", "kind": "declared", "why": "x"}])
    assert _order(conn, "Nobody") == ["Other", "Rel", "Cur"]
    # 两个都是 declared：Other 只有 0.01746，排在 Rel 后面——说明上面那条确实按本项目的倍数算
    _set_rel(conn, "Nobody", [{"project": "Other", "kind": "declared", "why": "x"},
                              {"project": "Rel", "kind": "declared", "why": "x"}])
    assert _order(conn, "Nobody") == ["Rel", "Other", "Cur"]


def test_incoming_edges_do_not_boost(conn):
    _same_text_docs(conn)
    _set_rel(conn, "Other", [{"project": "Nobody", "kind": "declared", "why": "别人指向我"}])  # Other→Nobody
    assert _order(conn, "Nobody") == ["Cur", "Rel", "Other"]  # 只看 Nobody 自己的出边：不加分


def test_related_boost_never_exceeds_project_boost(conn, trace_home):
    _same_text_docs(conn)
    _set_rel(conn, "Cur", [{"project": "Rel", "kind": "declared", "why": "x"}])
    trace_home.mkdir(parents=True, exist_ok=True)
    (trace_home / "config.toml").write_text("[search]\nrelated_boost = 5.0\n", encoding="utf-8")
    # 当前项目 Cur 排第 1（1/61*1.2），Rel 排第 2（1/62）；就算配了 5.0，也只按 project_boost 算，追不上
    assert _order(conn, "Cur")[0] == "Cur"


def test_related_boost_off_when_projmap_disabled(conn, trace_home):
    _same_text_docs(conn)
    _set_rel(conn, "Nobody", [{"project": "Rel", "kind": "declared", "why": "x"}])
    trace_home.mkdir(parents=True, exist_ok=True)
    (trace_home / "config.toml").write_text("[projmap]\nenabled = false\n", encoding="utf-8")
    assert _order(conn, "Nobody")[0] == "Cur"


# ---------------------------------------------------------------- janitor

def test_janitor_weekly_builds_map_and_isolates_errors(conn, trace_home, monkeypatch, tmp_path):
    w = make_world(tmp_path)
    use_map(trace_home, w.map)
    res = janitor.run(conn, config.load(), force=True, memroot=str(trace_home / "nomem"))
    assert res["projmap"] == 2 and edges(conn, "Delta") == {"Epsilon": ("declared", "同类：Epsilon")}

    def boom(*a, **k):
        raise RuntimeError("坏了")
    monkeypatch.setattr(projmap, "build", boom)
    res = janitor.run(conn, config.load(), force=True, memroot=str(trace_home / "nomem"))
    assert "坏了" in res["projmap_error"] and "projmap" not in res
    assert res["quick_check"] == "ok" and db.kv_get(conn, janitor.KEY_WEEKLY)  # 其他每周项和记录照常


def test_janitor_regular_run_rebuilds_only_when_files_changed(conn, trace_home, tmp_path):
    w = make_world(tmp_path)
    use_map(trace_home, w.map)
    now = time.time()
    janitor.run(conn, config.load(), now=now, force=True, memroot=str(trace_home / "nomem"))
    res = janitor.run(conn, config.load(), now=now + 7 * 3600, memroot=str(trace_home / "nomem"))
    assert res and "projmap" not in res  # 常规运行、文件没动：不重建
    write(w.d["Gamma"] / "CLAUDE.local.md", f"## 相关项目\n- `{w.d['Alpha']}`：客户端\n")
    res = janitor.run(conn, config.load(), now=now + 14 * 3600, memroot=str(trace_home / "nomem"))
    assert res["projmap"] == 3 and edges(conn, "Gamma") == {"Alpha": ("declared", "客户端")}


# ---------------------------------------------------------------- CLI

def test_cli_projmap_three_parts(conn, trace_home, tmp_path, capsys):
    w = make_world(tmp_path)
    use_map(trace_home, w.map)
    write(w.d["Beta"] / "CLAUDE.local.md", f"## 相关项目\n- `{w.d['Alpha']}`：电脑版\n")
    session(conn, "课程项目", mkdir(w.d["Alpha"], "docs", "课程项目"))  # 子目录
    for i in range(21):  # 库里有记录、地图里没有
        note(conn, "Orphan", f"孤儿项目的笔记 {i}")
    for i in range(20):  # 子目录项目笔记再多也不算"没有"
        note(conn, "课程项目", f"子目录的笔记 {i}")
    note(conn, "Few", "只有一条")
    # 候选：Gamma 改过 Beta 目录下 5 个文件、改过 Delta 下 4 个；Beta→Alpha 虽然也改了 6 个但地图里已有这条边
    note(conn, "Gamma", "改文件", files=[str(w.d["Beta"] / f"f{i}.kt") for i in range(5)]
         + [str(w.d["Delta"] / f"g{i}.rs") for i in range(4)])
    note(conn, "Beta", "改文件", files=[str(w.d["Alpha"] / f"h{i}.rs") for i in range(6)])
    assert cli.main(["projmap"]) == 0
    out = capsys.readouterr().out
    p1, rest = out.split("2. 库里有记录但地图里没有的项目")
    p2, p3 = rest.split("3. 跨项目改文件的候选")
    assert "Alpha → Beta（地图关联栏）；Gamma（" in p1
    assert "Beta → Alpha（电脑版）" in p1 and "Delta → Epsilon（同类：Epsilon）" in p1
    assert "Alpha ⇄ 课程项目（子目录）" in p1
    assert "Orphan  21 条" in p2 and "课程项目" not in p2 and "Few" not in p2
    assert "Gamma → Beta 改过 5 个文件" in p3 and "仅供参考" in p3
    assert "Gamma → Delta" not in p3 and "Beta → Alpha" not in p3  # 4 个不够；已在地图里
    assert projmap.related(conn, "Beta")  # 真的写了 kv
    # --project：第 1、3 部分只留以它为起点的，第 2 部分只留它自己
    assert cli.main(["projmap", "--project", "Gamma"]) == 0
    out = capsys.readouterr().out
    assert "Gamma → Beta 改过 5 个文件" in out and "Delta → Epsilon" not in out and "Orphan" not in out
    assert cli.main(["projmap", "--project", "Nope"]) == 0
    assert capsys.readouterr().out.split("1. 地图里的关系")[1].strip().startswith("无")


def test_cli_projmap_missing_map_and_disabled(conn, trace_home, tmp_path, capsys):
    use_map(trace_home, tmp_path / "no" / "such.md")
    _set_rel(conn, "Alpha", [{"project": "Beta", "kind": "declared", "why": "旧的"}])
    assert cli.main(["projmap"]) == 0
    assert "地图文件不存在或没有可解析的行" in capsys.readouterr().out
    assert projmap.related(conn, "Alpha") == []  # 旧的清掉
    (trace_home / "config.toml").write_text("[projmap]\nenabled = false\n", encoding="utf-8")
    assert cli.main(["projmap"]) == 0
    assert "已关闭" in capsys.readouterr().out


def test_dirs_listed_in_assoc_become_member_projects(tmp_path):
    """关联栏里列出、地图里没有单独一行的目录：算成员项目，和那一行互连，不占 projmap.max 名额；文件路径不算。"""
    w = World(tmp_path)
    hw = [mkdir(tmp_path, "ws", f"Hw{i}") for i in (1, 2, 3)]
    course = mkdir(tmp_path, "ws", "课程甲")
    listed = "、".join(f"`{h}`" for h in hw)
    w.write_map(f"| 课程甲 | `{course}` | 课程 | 作业工程：{listed}，用法照抄 `{hw[0]}\\CLAUDE.md`；参考 Gamma |\n")
    m = projmap.load_map(cfg(w.map, projmap__max=1))
    got = {e["project"]: e["why"] for e in m.edges["课程甲"]}
    assert set(got) == {"Hw1", "Hw2", "Hw3", "Gamma"}          # 3 个成员不受 max=1 截断
    assert got["Hw1"] == "地图关联栏"
    assert m.edges["Hw2"] == [{"project": "课程甲", "kind": "declared", "why": "列在 课程甲 的关联栏"}]
    assert "CLAUDE.md" not in m.nodes                          # 带扩展名的是文件
    assert "Hw1" not in {e["project"] for e in m.edges.get("Alpha", [])}
