"""向量检索一路：补向量、内存矩阵、候选过滤、与关键词的混合、fetch-model、记忆文件项目名。
全部用桩嵌入器，不依赖真实模型文件和 onnxruntime。"""

import hashlib
import json
import threading
import time

import pytest

import numpy as np

from knotcord import cli
from knotcord.core import config, db, lease as lease_mod
from knotcord.core.index import index_doc, remove_doc, set_doc_active
from knotcord.core.lease import Lease, LeaseLost
from knotcord.search import api, embed, memfiles, vectors
from test_search_api import add_note, add_summary, add_event, T0

DIM = 32


class StubEmb:
    """字二元组哈希成 DIM 维再归一化：共用字越多越相似，足够验证排序和过滤。"""
    dim = DIM

    def __init__(self, on_encode=None):
        self.calls = []
        self.on_encode = on_encode

    @staticmethod
    def _vec(t):
        v = np.zeros(DIM, dtype=np.float32)
        t = t or " "
        for a, b in zip(t, t[1:] or t):
            v[int(hashlib.md5((a + b).encode()).hexdigest(), 16) % DIM] += 1.0
        n = float(np.linalg.norm(v))
        return v / (n or 1.0)

    def encode(self, texts, batch_size=32):
        self.calls.append(list(texts))
        if self.on_encode:
            self.on_encode()
        return np.stack([self._vec(t) for t in texts]).astype(np.float32)

    def encode_query(self, text):
        return self.encode([text])[0]


@pytest.fixture
def stub(monkeypatch):
    emb = StubEmb()
    vectors.release()
    monkeypatch.setattr(embed, "model_present", lambda: True)
    monkeypatch.setattr(embed, "get", lambda cfg=None, *, bulk=False: emb)
    monkeypatch.setattr(embed, "release", lambda *, bulk=None: True)
    yield emb
    vectors.release()


@pytest.fixture
def cfg():
    return config.load()


@pytest.fixture
def lease(conn):
    le = Lease(conn, owner="me", ttl_s=90)
    assert le.try_acquire()
    return le


def run_backfill(conn, cfg, lease, **kw):
    return vectors.backfill(conn, cfg, lease, threading.Event(), **kw)


def vec_ids(conn):
    return [r[0] for r in conn.execute("SELECT doc_id FROM docs_vec ORDER BY doc_id")]


# ---------------------------------------------------------------- 文本口径

def test_doc_texts_per_source(conn, tmp_path):
    n = add_note(conn, "宝塔反代", "正文内容", facts=["事实一", "事实二"])
    s = add_summary(conn, "配置面板", "完成反代")
    p = add_event(conn, "prompt", "提示原文", index=True)
    f = tmp_path / "m.md"
    f.write_text("---\nname: 记忆名\ndescription: 记忆描述\n---\n记忆正文\n", encoding="utf-8")
    conn.execute("INSERT INTO memfiles(path, project, mtime, name, description) VALUES(?,?,?,?,?)",
                 (str(f), "Span", 1, "登记名", "登记描述"))
    mid = conn.execute("SELECT id FROM memfiles").fetchone()[0]
    index_doc(conn, "memfile", mid, "Span", 1, "x")
    rows = conn.execute("SELECT id, src, src_id FROM docs").fetchall()
    got = vectors.doc_texts(conn, [(r["id"], r["src"], r["src_id"]) for r in rows])
    by_src = {r["src"]: got[r["id"]] for r in rows}
    assert "宝塔反代" in by_src["note"] and "事实二" in by_src["note"] and "a.py" not in by_src["note"]
    assert "gotcha" in by_src["note"]  # tags 进文本，files 不进
    assert "配置面板" in by_src["summary"] and "完成反代" in by_src["summary"] and "学到了" in by_src["summary"]
    assert by_src["prompt"] == "提示原文"
    assert "记忆名" in by_src["memfile"] and "记忆正文" in by_src["memfile"]
    # 记忆文件没了：退回登记的名字和描述
    f.unlink()
    got = vectors.doc_texts(conn, [(r["id"], r["src"], r["src_id"]) for r in rows if r["src"] == "memfile"])
    assert list(got.values()) == ["登记名\n登记描述"]
    # 孤儿索引（原行不存在）不出现在结果里
    assert vectors.doc_texts(conn, [(999, "note", 12345)]) == {}


# ---------------------------------------------------------------- 补向量

def test_backfill_writes_all_and_counts(conn, cfg, lease, stub):
    ids = [add_note(conn, f"笔记{i}", f"内容{i}") for i in range(5)]
    assert vectors.missing_count(conn) == 5 and vectors.has_missing(conn)
    n, more = run_backfill(conn, cfg, lease)
    assert (n, more) == (5, False)
    assert vectors.missing_count(conn) == 0 and not vectors.has_missing(conn)
    assert len(vec_ids(conn)) == 5
    row = conn.execute("SELECT model, vec FROM docs_vec").fetchone()
    assert row["model"] == embed.MODEL_ID and len(row["vec"]) == DIM * 4
    # 再跑一次什么都不做，不重复嵌
    calls = len(stub.calls)
    assert run_backfill(conn, cfg, lease) == (0, False)
    assert len(stub.calls) == calls
    assert ids  # 避免未使用告警


def test_backfill_round_cap_and_batch(conn, cfg, lease, stub):
    for i in range(7):
        add_note(conn, f"笔记{i}", "内容")
    n, more = run_backfill(conn, cfg, lease, max_docs=4)
    assert n >= 4 and more is True  # 本轮达到上限，后面还有
    assert all(len(c) <= int(cfg.get("embed.batch")) for c in stub.calls)
    n2, more2 = run_backfill(conn, cfg, lease)
    assert more2 is False and vectors.missing_count(conn) == 0
    assert n + n2 == 7


def test_backfill_skips_orphans_and_inactive(conn, cfg, lease, stub):
    add_note(conn, "有效", "x")
    add_note(conn, "已取代", "y", state="superseded")  # active=0 不嵌
    conn.execute("INSERT INTO docs(src, src_id, project, ts, active) VALUES('note', 9999, 'Span', 1, 1)")  # 孤儿
    n, more = run_backfill(conn, cfg, lease)
    assert n == 1 and more is False
    assert len(vec_ids(conn)) == 1


def test_backfill_stops_on_event(conn, cfg, lease, stub):
    total = 3 * int(cfg.get("embed.batch"))
    for i in range(total):
        add_note(conn, f"笔记{i}", "内容")
    ev = threading.Event()
    stub.on_encode = ev.set  # 第一批嵌完就叫停
    n, more = vectors.backfill(conn, cfg, lease, ev)
    assert 0 < n < total and more is False  # 被叫停：返回 more=False，由下一轮（新 stop）再说
    assert vectors.missing_count(conn) == total - n
    ev2 = threading.Event()
    ev2.set()
    assert vectors.backfill(conn, cfg, lease, ev2) == (0, False)  # 已置位：一条都不做


def test_backfill_respects_lease(conn, cfg, stub, trace_home):
    add_note(conn, "a", "b")
    other = db.connect()
    try:
        Lease(other, owner="other", ttl_s=90).try_acquire()
        mine = Lease(conn, owner="me", ttl_s=90)
        assert run_backfill(conn, cfg, mine) == (0, False)  # 开头就发现不是自己的
        assert vec_ids(conn) == []
    finally:
        other.close()

    class Flaky:
        """开头看着是自己的，写事务里才发现租约丢了。"""
        def is_mine(self):
            return True

        def assert_mine_in_tx(self):
            raise LeaseLost()

    assert run_backfill(conn, cfg, Flaky()) == (0, False)
    assert vec_ids(conn) == [] and not conn.in_transaction


def test_backfill_noop_when_model_absent_or_disabled(conn, cfg, lease, stub, monkeypatch):
    add_note(conn, "a", "b")
    monkeypatch.setattr(embed, "model_present", lambda: False)
    assert run_backfill(conn, cfg, lease) == (0, False)
    monkeypatch.setattr(embed, "model_present", lambda: True)
    monkeypatch.setattr(embed, "get", lambda cfg=None, *, bulk=False: None)  # 加载失败
    assert run_backfill(conn, cfg, lease) == (0, False)
    off = config.Config({**{k: cfg.get(k) for k in config.DEFAULTS}, "search.vector": False})
    assert run_backfill(conn, off, lease) == (0, False)
    assert vec_ids(conn) == []


def test_embed_pass_backs_off_on_error(conn, cfg, lease, stub, monkeypatch):
    from knotcord import work
    monkeypatch.setattr(work, "_embed_fail_until", 0.0)
    monkeypatch.setattr(work, "_embed_next", 0.0)
    add_note(conn, "a", "b")

    def boom(*a, **k):
        raise RuntimeError("坏了")

    monkeypatch.setattr(vectors, "backfill", boom)
    assert work.embed_pass(conn, cfg, lease, threading.Event()) == (0, False)
    assert work._embed_fail_until > time.time()
    assert work.embed_due(conn, cfg) is False  # 退避期内不去抢租约


# ---------------------------------------------------------------- 内存矩阵与候选

def test_candidates_rank_and_filters(conn, lease, stub):
    cfg = config.Config({**{k: config.DEFAULTS[k] for k in config.DEFAULTS}, "search.vector_min": 0.0})  # 桩的余弦普遍低，不设下限
    a = add_note(conn, "宝塔面板反向代理配置", "nginx 反代 域名", project="Span")
    b = add_note(conn, "苹果派的做法", "面粉 黄油", project="Span")
    c = add_note(conn, "宝塔面板反向代理旧版", "nginx 反代 域名", project="Other")
    d = add_note(conn, "宝塔面板反向代理被取代", "nginx 反代 域名", project="Span")
    run_backfill(conn, cfg, lease)
    set_doc_active(conn, "note", d, False)  # 嵌好之后才被取代：向量还在，检索时按 active 过滤
    doc = {r["src_id"]: r["id"] for r in conn.execute("SELECT id, src_id FROM docs")}
    got = vectors.candidates(conn, "宝塔面板反向代理", project=None, limit=10, include_superseded=False, cfg=cfg)
    ids = [i for i, _ in got]
    assert ids[0] in (doc[a], doc[c]) and doc[b] == ids[-1]
    assert doc[d] not in ids  # 已取代的默认不出
    assert all(got[i][1] >= got[i + 1][1] for i in range(len(got) - 1))  # 从高到低
    only = vectors.candidates(conn, "宝塔面板反向代理", project="Span", limit=10, include_superseded=False, cfg=cfg)
    assert {i for i, _ in only} == {doc[a], doc[b]}
    # 过滤发生在取前 N 之前：limit=1 时项目过滤后仍有结果，不被别的项目占满名额
    one = vectors.candidates(conn, "宝塔面板反向代理", project="Span", limit=1, include_superseded=False, cfg=cfg)
    assert len(one) == 1 and one[0][0] == doc[a]
    inc = vectors.candidates(conn, "宝塔面板反向代理", project="Span", limit=10, include_superseded=True, cfg=cfg)
    assert doc[d] in {i for i, _ in inc}


def test_candidates_floor_and_failure(conn, cfg, lease, stub, monkeypatch):
    add_note(conn, "宝塔面板", "反代")
    run_backfill(conn, cfg, lease)
    hi = config.Config({**{k: cfg.get(k) for k in config.DEFAULTS}, "search.vector_min": 0.999})
    assert vectors.candidates(conn, "完全无关的内容", project=None, limit=5, include_superseded=False, cfg=hi) == []
    monkeypatch.setattr(vectors, "get_index", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert vectors.candidates(conn, "宝塔", project=None, limit=5, include_superseded=False, cfg=cfg) == []


def test_index_appends_then_reloads_on_invalidation(conn, cfg, lease, stub):
    n1 = add_note(conn, "第一条", "内容一")
    run_backfill(conn, cfg, lease)
    ids, mat, n = vectors.get_index(conn, DIM)
    assert n == 1
    first = vectors._idx
    n2 = add_note(conn, "第二条", "内容二")
    run_backfill(conn, cfg, lease)
    _, _, n = vectors.get_index(conn, DIM)
    assert n == 2 and vectors._idx is first  # 追加：同一个对象，没有整体重读
    # 文本改了：旧向量作废，kv 计数 +1，下次取矩阵整体重读，作废的那条不在里面
    doc1 = conn.execute("SELECT id FROM docs WHERE src_id=?", (n1,)).fetchone()[0]
    index_doc(conn, "note", n1, "Span", T0, "改写之后的全新文本")
    assert vectors.missing_count(conn) == 1
    assert doc1 not in vec_ids(conn)
    ids, _, n = vectors.get_index(conn, DIM)
    assert n == 1 and vectors._idx is not first and doc1 not in set(ids[:n].tolist())
    run_backfill(conn, cfg, lease)  # 补回去：doc_id 比已有最大的小，补向量时通知重读
    ids, _, n = vectors.get_index(conn, DIM)
    assert n == 2 and set(ids[:n].tolist()) == set(vec_ids(conn))
    assert n2


def test_remove_doc_drops_vector(conn, cfg, lease, stub):
    n1 = add_note(conn, "要删的", "内容")
    add_note(conn, "留着的", "内容")
    run_backfill(conn, cfg, lease)
    remove_doc(conn, "note", n1)
    assert len(vec_ids(conn)) == 1
    _, _, n = vectors.get_index(conn, DIM)
    assert n == 1


def test_model_name_change_makes_vectors_missing(conn, cfg, lease, stub, monkeypatch):
    add_note(conn, "a", "b")
    run_backfill(conn, cfg, lease)
    assert vectors.missing_count(conn) == 0
    monkeypatch.setattr(embed, "MODEL_ID", "other-model")
    assert vectors.missing_count(conn) == 1
    assert run_backfill(conn, cfg, lease) == (1, False)  # 换了模型名：旧向量被覆盖重嵌
    assert conn.execute("SELECT model FROM docs_vec").fetchone()[0] == "other-model"


# ---------------------------------------------------------------- 与关键词混合

class ConceptEmb(StubEmb):
    """按"概念词"分组：含 反代/代理/nginx 的是第 0 维，含 转发 的也算第 0 维（模拟同义），含 苹果 的第 1 维。"""
    GROUPS = [("反代", "代理", "nginx", "转发"), ("苹果", "面粉")]

    @classmethod
    def _vec(cls, t):
        v = np.zeros(DIM, dtype=np.float32)
        for i, words in enumerate(cls.GROUPS):
            if any(w in (t or "") for w in words):
                v[i] = 1.0
        v[DIM - 1] = 0.01
        return v / float(np.linalg.norm(v))


def write_config(trace_home, text):
    trace_home.mkdir(parents=True, exist_ok=True)
    (trace_home / "config.toml").write_text(text, encoding="utf-8")


def test_search_hybrid_finds_what_keywords_miss(conn, cfg, lease, trace_home, monkeypatch):
    emb = ConceptEmb()
    vectors.release()
    monkeypatch.setattr(embed, "model_present", lambda: True)
    monkeypatch.setattr(embed, "get", lambda cfg=None, *, bulk=False: emb)
    target = add_note(conn, "宝塔面板反代配置", "nginx 反代 域名 证书")
    add_note(conn, "苹果派做法", "面粉 黄油 烤箱")
    run_backfill(conn, cfg, lease)
    q = "服务器入口转发怎么弄"  # 和笔记没有任何共同的字二元组；意思相近靠向量
    write_config(trace_home, "[search]\nvector = false\n")
    base = api.search(conn, q)
    assert f"n{target}" not in base
    write_config(trace_home, "[search]\nvector = true\n")
    hybrid = api.search(conn, q)
    assert hybrid.splitlines()[0].startswith(f"n{target} ")
    vectors.release()


def test_search_unchanged_when_vector_unavailable(conn, cfg, lease, stub, trace_home, monkeypatch):
    add_note(conn, "宝塔面板反代", "nginx")
    add_note(conn, "宝塔备份", "定时备份")
    add_summary(conn, "宝塔面板", "完成")
    run_backfill(conn, cfg, lease)
    write_config(trace_home, "[search]\nvector = false\n")
    expected = api.search(conn, "宝塔面板")
    assert expected != "无结果"
    write_config(trace_home, "[search]\nvector = true\n")
    # 模型不在
    monkeypatch.setattr(embed, "get", lambda cfg=None, *, bulk=False: None)
    assert api.search(conn, "宝塔面板") == expected
    # 向量表是空的
    vectors.release()
    conn.execute("DELETE FROM docs_vec")
    monkeypatch.setattr(embed, "get", lambda cfg=None, *, bulk=False: StubEmb())
    assert api.search(conn, "宝塔面板") == expected


# ---------------------------------------------------------------- doctor / fetch-model

def test_doctor_counts_missing_by_model(trace_home, conn, cfg, lease, stub, capsys, monkeypatch):
    add_note(conn, "a", "b")
    add_note(conn, "c", "d")
    run_backfill(conn, cfg, lease)
    assert cli.main(["doctor"]) == 0
    assert "缺向量 0 条" in capsys.readouterr().out
    conn.execute("UPDATE docs_vec SET model='旧模型' WHERE doc_id=(SELECT min(doc_id) FROM docs_vec)")
    assert cli.main(["doctor"]) == 0
    assert "缺向量 1 条" in capsys.readouterr().out


def test_fetch_model_idempotent_and_verifies(trace_home, monkeypatch):
    payload = {"model_quantized.onnx": b"m" * 2000, "tokenizer.json": b"{}"}
    files = {k: (k, len(v), hashlib.sha256(v).hexdigest()) for k, v in payload.items()}
    monkeypatch.setattr(embed, "FILES", files)
    fetched = []

    def fake_download(url, dest, size, sha, log_fn):
        fetched.append(url)
        dest.write_bytes(payload[dest.name])

    class OkEmb:
        dim = DIM

        def __init__(self, *a, **k):
            pass

        def encode(self, texts, batch_size=32):
            return np.ones((len(texts), DIM), dtype=np.float32) / np.sqrt(DIM)

    monkeypatch.setattr(embed, "_download", fake_download)
    monkeypatch.setattr(embed, "Embedder", OkEmb)
    logs = []
    assert embed.fetch_model(logs.append) == 0
    assert len(fetched) == 2 and (embed.model_dir() / "tokenizer.json").read_bytes() == b"{}"
    assert embed.fetch_model(logs.append) == 0
    assert len(fetched) == 2  # 第二次文件齐全、校验通过：不再下载
    assert any("已就绪" in x for x in logs)
    # 文件被改坏：重新下载
    (embed.model_dir() / "tokenizer.json").write_bytes(b"broken")
    assert embed.fetch_model(logs.append) == 0 and len(fetched) == 3


def test_fetch_model_falls_back_to_mirror_and_reports_failure(trace_home, monkeypatch):
    payload = {"model_quantized.onnx": b"m" * 2000, "tokenizer.json": b"{}"}
    monkeypatch.setattr(embed, "FILES", {k: (k, len(v), hashlib.sha256(v).hexdigest()) for k, v in payload.items()})
    seen = []

    def fake_download(url, dest, size, sha, log_fn):
        seen.append(url)
        if url.startswith("https://huggingface.co"):
            raise OSError("直连不通")
        dest.write_bytes(payload[dest.name])

    class OkEmb:
        dim = DIM

        def __init__(self, *a, **k):
            pass

        def encode(self, texts, batch_size=32):
            return np.ones((len(texts), DIM), dtype=np.float32)

    monkeypatch.setattr(embed, "_download", fake_download)
    monkeypatch.setattr(embed, "Embedder", OkEmb)
    assert embed.fetch_model(lambda s: None) == 0
    assert any(u.startswith("https://hf-mirror.com") for u in seen)

    def always_fail(url, dest, size, sha, log_fn):
        raise OSError("都不通")

    (embed.model_dir() / "tokenizer.json").unlink()
    monkeypatch.setattr(embed, "_download", always_fail)
    assert embed.fetch_model(lambda s: None) == 1


def test_cli_fetch_model_dispatch(trace_home, monkeypatch):
    monkeypatch.setattr(embed, "fetch_model", lambda log_fn=print: 7)
    assert cli.main(["fetch-model"]) == 7


# ---------------------------------------------------------------- 记忆文件的项目名

def test_encode_cwd():
    assert memfiles.encode_cwd(r"E:\ai-guardian-star") == "E--ai-guardian-star"
    assert memfiles.encode_cwd(r"D:\user\桌面 x") == "D--user----x"


def test_resolve_project_prefers_sessions_then_fallback(conn):
    conn.execute("INSERT INTO sessions(id, client, project, cwd, last_seen) VALUES('s1','claude','ai-guardian-star',?,1)",
                 (r"Z:\no-such-drive\ai-guardian-star",))
    known = memfiles.session_projects(conn)
    assert known == {"Z--no-such-drive-ai-guardian-star": "ai-guardian-star"}
    assert memfiles.resolve_project("Z--no-such-drive-ai-guardian-star", known) == "ai-guardian-star"
    # 没有记录、磁盘上也找不到：退回旧规则（最后一段）
    assert memfiles.resolve_project("Z--no-such-drive-other-proj", known) == "proj"


def test_resolve_project_walks_disk(tmp_path):
    repo = tmp_path / "my-hyphen-repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    enc = memfiles.encode_cwd(str(repo))
    # 子目录的记忆目录名也能找回，并向上找到 .git 所在目录取名
    assert memfiles.resolve_project(enc) == "my-hyphen-repo"
    assert memfiles.resolve_project(memfiles.encode_cwd(str(repo / "sub"))) == "my-hyphen-repo"
    # 同一编码对应两个真实目录：有歧义，不乱猜，退回旧规则
    (tmp_path / "my.hyphen.repo").mkdir()
    assert memfiles.resolve_project(enc) == memfiles.project_from_dir(enc)


def test_reindex_corrects_project_without_reparse(conn, tmp_path):
    root = tmp_path / "projects"
    mem = root / "Z--no-such-drive-ai-guardian-star" / "memory"
    mem.mkdir(parents=True)
    f = mem / "a.md"
    f.write_text("---\nname: n\ndescription: d\n---\n正文\n", encoding="utf-8")
    assert memfiles.reindex(conn, root=str(root)) == 1
    assert conn.execute("SELECT project FROM memfiles").fetchone()[0] == "star"  # 旧规则的截断结果
    # sessions 里记下了真实项目名：文件没变，只改项目名（不重读文件）
    conn.execute("INSERT INTO sessions(id, client, project, cwd, last_seen) VALUES('s1','claude','ai-guardian-star',?,1)",
                 (r"Z:\no-such-drive\ai-guardian-star",))
    assert memfiles.reindex(conn, root=str(root)) == 1
    assert conn.execute("SELECT project FROM memfiles").fetchone()[0] == "ai-guardian-star"
    assert conn.execute("SELECT project FROM docs WHERE src='memfile'").fetchone()[0] == "ai-guardian-star"
    assert memfiles.reindex(conn, root=str(root)) == 0  # 稳定后不再动
