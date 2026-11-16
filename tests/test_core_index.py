import pytest

from knotcord.core import db, index

DOCS = {
    1: "宝塔面板安装在 VPS1 上，端口 8888，用来管理网站",
    2: "3x-ui 面板的路由规则配置：geosite 直连，其余走代理",
    3: "ICT 收集表 填写说明：学号姓名都要写",
    4: "Span 的路由模块重构，拆成三个文件",
    5: "宝塔里给 3x-ui 做了反代配置",
    6: "今天吃了面条，没有别的事",
}


@pytest.fixture
def idx(conn):
    for i, text in DOCS.items():
        index.index_doc(conn, "n", i, "proj", 1000 + i, text)
    return conn


def src_ids(rows):
    return [r["src_id"] for r in rows]


def test_segment():
    assert index.segment("宝塔面板") == "宝塔 塔面 面板"
    assert index.segment("表") == "表"
    assert index.segment("路由") == "路由"
    assert index.segment("ICT 收集表") == "ict 收集 集表"
    assert index.segment("3x-ui") == "3x ui"
    assert index.segment("宝塔，面板") == "宝塔 面板"
    assert index.segment("ＩＣＴ") == "ict"  # 全角转半角
    assert index.segment("") == ""


def test_basic_hits(idx):
    assert set(src_ids(index.fts_search(idx, "宝塔"))) == {1, 5}
    assert set(src_ids(index.fts_search(idx, "路由"))) == {2, 4}
    assert set(src_ids(index.fts_search(idx, "3x-ui"))) == {2, 5}
    assert src_ids(index.fts_search(idx, "宝塔面板"))[0] == 1
    assert src_ids(index.fts_search(idx, "ICT 收集表"))[0] == 3


def test_and_then_relax(idx):
    rows = index.fts_search(idx, "3x-ui 路由")
    ids = src_ids(rows)
    assert ids[0] == 2  # 两个词都含的排第一
    assert {2, 4, 5} <= set(ids)  # 严格命中不足 3 条，放宽成 OR 补进来
    assert 6 not in ids


def test_columns_and_score(idx):
    rows = index.fts_search(idx, "宝塔")
    assert set(rows[0].keys()) == {"doc_id", "src", "src_id", "project", "ts", "score"}
    assert rows[0]["src"] == "n" and rows[0]["project"] == "proj"
    assert isinstance(rows[0]["score"], float)
    assert rows[0]["score"] >= rows[-1]["score"]


@pytest.mark.parametrize("q", [
    '"', "*", "(", ")", "AND", "OR", "NOT", "NEAR(", "NEAR(a b)", "a OR", "-", "^foo", "col:foo",
    "seg:宝塔", "'; DROP TABLE docs; --", '"unterminated', "宝塔 AND", "\x00", "宝\ud800塔", "{}[]", "%_",
    "  ", "a" * 5000, "宝" * 3000, "3x-", "-3x", "a-b-c-d-e-f", "NOT 宝塔", "宝塔 NEAR/3 面板",
])
def test_hostile_queries_do_not_raise(idx, q):
    rows = index.fts_search(idx, q)
    assert isinstance(rows, list)


def test_single_han_returns_empty(idx):
    assert index.fts_search(idx, "表") == []
    assert index.fts_search(idx, "表 的") == []
    assert index.fts_search(idx, "") == []
    # 单字夹在别的词里：忽略单字，用其余词搜
    assert 3 in src_ids(index.fts_search(idx, "ICT 表"))


def test_set_doc_active(idx):
    index.set_doc_active(idx, "n", 1, False)
    assert 1 not in src_ids(index.fts_search(idx, "宝塔"))
    assert 1 in src_ids(index.fts_search(idx, "宝塔", active_only=False))
    index.set_doc_active(idx, "n", 1, True)
    assert 1 in src_ids(index.fts_search(idx, "宝塔"))


def test_reindex_replaces_old_terms(idx):
    d1 = index.index_doc(idx, "n", 1, "proj", 2000, "完全换了内容 只剩 nginx")
    assert d1 == idx.execute("SELECT id FROM docs WHERE src='n' AND src_id=1").fetchone()[0]
    assert idx.execute("SELECT count(*) FROM docs WHERE src='n' AND src_id=1").fetchone()[0] == 1
    assert 1 not in src_ids(index.fts_search(idx, "宝塔"))
    assert src_ids(index.fts_search(idx, "nginx")) == [1]


def test_remove_doc(idx):
    index.remove_doc(idx, "n", 1)
    assert 1 not in src_ids(index.fts_search(idx, "宝塔"))
    assert idx.execute("SELECT count(*) FROM docs WHERE src_id=1").fetchone()[0] == 0
    index.remove_doc(idx, "n", 999)  # 不存在也不报错


def test_project_and_srcs_filter(idx):
    index.index_doc(idx, "s", 1, "other", 5000, "宝塔 的总结")
    rows = index.fts_search(idx, "宝塔", project="other")
    assert [(r["src"], r["src_id"]) for r in rows] == [("s", 1)]
    assert set(src_ids(index.fts_search(idx, "宝塔", srcs=("n",)))) == {1, 5}
    assert len(index.fts_search(idx, "宝塔")) == 3  # 不过滤时都在


def test_filter_before_limit(conn):
    # 停用的文档不能把 limit 占满
    for i in range(10):
        index.index_doc(conn, "n", i, "p", i, "宝塔 旧版本", active=False)
    index.index_doc(conn, "n", 100, "p", 1, "宝塔 新版本")
    assert src_ids(index.fts_search(conn, "宝塔", limit=3)) == [100]


def test_index_doc_joins_outer_tx_and_rolls_back(conn):
    with pytest.raises(RuntimeError):
        with db.write_tx(conn):
            index.index_doc(conn, "n", 1, "p", 1, "宝塔")
            raise RuntimeError("x")
    assert conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 0
    assert index.fts_search(conn, "宝塔") == []
    assert conn.execute("SELECT count(*) FROM docs_fts_docsize").fetchone()[0] == 0
