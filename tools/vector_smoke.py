"""向量检索冒烟：用真实模型（~/.knotcord/models/ 里的 bge-small-zh-v1.5 int8）走一遍补向量 + 混合检索。

需要先 `python -I -X utf8 knotcord\\boot.py fetch-model`。只读真实目录里的模型文件；
库建在临时目录里，跑完删除，不碰真实的 ~/.knotcord/knotcord.db。
运行（在仓库根目录）：python -I -X utf8 tools\\vector_smoke.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from knotcord import boot  # noqa: E402

boot._setup_path()  # 和 server 一样：加上 ~/.knotcord/venv 和用户 site（-I 模式下默认看不到）

from knotcord.search import embed  # noqa: E402

MODEL_DIR = embed.model_dir()  # 先取真实位置，再把 KNOTCORD_HOME 指到临时目录


def main() -> int:
    if not embed.model_present():
        print("没有模型文件，先运行 boot.py fetch-model")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="knotcord_smoke_"))
    os.environ["KNOTCORD_HOME"] = str(tmp)
    try:
        from knotcord.core import config, db, lease as lease_mod
        from knotcord.core.index import index_doc
        from knotcord.search import api, vectors

        t0 = time.time()
        emb = embed.Embedder(MODEL_DIR, max_tokens=256, threads=4)
        print(f"模型加载 {time.time() - t0:.2f}s，维度 {emb.dim}")
        v = emb.encode(["今天天气不错", "宝塔面板怎么反向代理"])
        norms = [float((x ** 2).sum()) ** 0.5 for x in v]
        assert all(abs(n - 1) < 1e-3 for n in norms), f"向量没有归一化: {norms}"

        # 让 vectors/embed 用这个已加载的实例，模型路径指回真实目录
        embed._inst[False] = emb
        embed._inst[True] = emb
        embed.model_present = lambda: True

        conn = db.connect()
        docs = [
            ("宝塔面板反代配置", "给网站配置 nginx 反向代理，转发到本机 8080 端口，证书用 certbot 自动续期"),
            ("代理软件频繁断连的原因", "TUN 模式下 DNS 劫持冲突，虚拟网卡被系统休眠回收，导致连接中断"),
            ("苹果派的做法", "面粉、黄油、苹果切片，烤箱 180 度 40 分钟"),
        ]
        ids = []
        for i, (title, body) in enumerate(docs):
            cur = conn.execute("INSERT INTO notes(project, session_id, ts, type, title, body, facts, files, tags, state) "
                               "VALUES('Smoke','s',?, 'discovery', ?, ?, '[]', '[]', '[]', 'active')",
                               (1_762_700_000 + i, title, body))
            index_doc(conn, "note", cur.lastrowid, "Smoke", 1_762_700_000 + i, f"{title}\n{body}")
            ids.append(cur.lastrowid)
        cfg = config.load()
        lease = lease_mod.Lease(conn, owner="smoke", ttl_s=300)
        assert lease.try_acquire()
        n, more = vectors.backfill(conn, cfg, lease, threading.Event())
        print(f"补向量 {n} 条，还有: {more}，缺向量 {vectors.missing_count(conn)} 条")
        assert n == len(docs) and vectors.missing_count(conn) == 0

        failed = 0
        for q, want in (("服务器面板转发怎么配", ids[0]), ("科学上网工具老是掉线", ids[1])):
            t0 = time.time()
            out = api.search(conn, q, limit=3)
            dt = (time.time() - t0) * 1000
            first = out.splitlines()[0] if out else ""
            ok = first.startswith(f"n{want} ")
            failed += 0 if ok else 1
            print(f"{'通过' if ok else '失败'} 「{q}」 {dt:.0f} ms 首条: {first[:60]}")
        print("冒烟通过" if not failed else f"冒烟失败 {failed} 项")
        conn.close()
        return 1 if failed else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
