"""本地嵌入：bge-small-zh-v1.5 的 int8 ONNX，只用 onnxruntime + tokenizers + numpy（不装 torch）。

- 模型放 ~/.knotcord/models/bge-small-zh-v1.5-int8/；`boot.py fetch-model` 下载并校验（幂等）。
- 重依赖全部在函数里延迟 import；模型文件不在、依赖没装、加载失败，get() 都返回 None，
  调用方（检索、补向量）据此整条向量路静默关闭，关键词检索照常。
- 池化按 bge 的要求用 [CLS]（模型卡 1_Pooling: pooling_mode_cls_token），再 L2 归一化；
  查询加指令前缀，文档不加。
- 模型和 tokenizer 常驻在进程里（一个单例）；search.unload_minutes 分钟没人用，由 worker 心跳释放。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import urllib.request
from pathlib import Path

from knotcord.core import log, paths

_log = log.get_logger("embed")

MODEL_ID = "bge-small-zh-v1.5-int8"  # 写进 docs_vec.model；换模型就换这个名字，旧向量自然变成"缺"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
EXPECTED_DIM = 512  # 方案书写的维度；以实测为准（Embedder.dim），不一致时只记日志

REPO = "Xenova/bge-small-zh-v1.5"
HOSTS = ("https://huggingface.co", "https://hf-mirror.com")
# 文件名 -> (仓库内路径, 字节数, sha256)。model 的 sha256 取自仓库 LFS 元数据；
# tokenizer.json 的 sha256 是下载后实算的，同时核对过它的 git blob oid 等于仓库 API 给的值
FILES = {
    "model_quantized.onnx": ("onnx/model_quantized.onnx", 24010842,
                             "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc"),
    "tokenizer.json": ("tokenizer.json", 439125,
                       "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"),
}


def model_dir() -> Path:
    return paths.home() / "models" / MODEL_ID


def model_paths() -> tuple[Path, Path]:
    d = model_dir()
    return d / "model_quantized.onnx", d / "tokenizer.json"


def model_present() -> bool:
    """只看文件在不在（便宜，可以每次检索都调）。"""
    m, t = model_paths()
    try:
        return m.is_file() and t.is_file() and m.stat().st_size > 1_000_000
    except OSError:
        return False


# ---------------------------------------------------------------- 下载

def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_ok(p: Path, size: int, sha: str) -> bool:
    try:
        return p.is_file() and p.stat().st_size == size and _sha256(p) == sha
    except OSError:
        return False


def _download(url: str, dest: Path, size: int, sha: str, log_fn) -> None:
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "knotcord-fetch-model"})
    h = hashlib.sha256()
    n = 0
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                n += len(chunk)
                if n > size * 1.05:
                    raise ValueError("下载量超过预期大小")
        if n != size:
            raise ValueError(f"大小不对: 得到 {n}，应为 {size}")
        if h.hexdigest() != sha:
            raise ValueError("sha256 不一致")
        os.replace(part, dest)
        log_fn(f"  已下载 {dest.name} {n / 1048576:.1f} MB，用时 {time.time() - t0:.1f} 秒")
    finally:
        try:
            part.unlink()
        except OSError:
            pass


def fetch_model(log_fn=print) -> int:
    """下载模型和 tokenizer 到 ~/.knotcord/models/<MODEL_ID>/ 并校验；已就绪就什么都不做。0 成功。"""
    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name, (rel, size, sha) in FILES.items():
        dest = d / name
        if _file_ok(dest, size, sha):
            log_fn(f"  {name} 已就绪（{size / 1048576:.1f} MB，sha256 核对通过）")
            continue
        err = None
        for host in HOSTS:
            url = f"{host}/{REPO}/resolve/main/{rel}"
            log_fn(f"  下载 {url}")
            try:
                _download(url, dest, size, sha, log_fn)
                err = None
                break
            except Exception as e:  # 直连不通换镜像
                err = e
                log_fn(f"  失败: {type(e).__name__}: {str(e)[:120]}")
        if err is not None:
            log_fn(f"下载 {name} 失败，所有来源都试过了")
            return 1
    try:
        emb = Embedder(model_dir(), max_tokens=64, threads=2)  # 查询用的那种配置
        v = emb.encode(["今天天气不错"])
        log_fn(f"加载自检通过：向量维度 {emb.dim}，范数 {float((v[0] ** 2).sum()) ** 0.5:.4f}")
        if emb.dim != EXPECTED_DIM:
            log_fn(f"注意：实测维度 {emb.dim} 与文档写的 {EXPECTED_DIM} 不一致，以实测为准")
    except Exception as e:
        log_fn(f"文件已就位但加载失败（onnxruntime/tokenizers/numpy 是否装好？）: {type(e).__name__}: {e}")
        return 2
    return 0


# ---------------------------------------------------------------- 模型

class Embedder:
    """一个加载好的模型。encode 线程安全（onnxruntime 允许并发 run；tokenizer 配置只在构造时设一次）。"""

    def __init__(self, directory: Path, *, max_tokens: int = 256, threads: int = 4, bulk: bool = False) -> None:
        """bulk=True 是补向量用的（开内存池，快约 1.7 倍，批大小 8 时多占约 100 MB，补完就释放）；
        默认（查询用）关内存池：单条短句不需要，常驻时只多占几十 MB。"""
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._np = np
        model_file = Path(directory) / "model_quantized.onnx"
        tok = Tokenizer.from_file(str(Path(directory) / "tokenizer.json"))
        tok.enable_truncation(max_length=int(max_tokens))
        pad_id = tok.token_to_id("[PAD]")
        # 批量时补到 64 的整数倍：输入形状只有几种，onnxruntime 缓存的东西不会随长度乱涨
        tok.enable_padding(pad_id=0 if pad_id is None else pad_id, pad_token="[PAD]",
                           pad_to_multiple_of=64 if bulk else None)
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, int(threads))
        so.inter_op_num_threads = 1
        so.log_severity_level = 3
        # 实测（256 token、批 32，输入长度每批都变）：开内存池 RSS 涨到 580 MB、512 token 时 1.6 GB；
        # 关掉后稳定在 125 MB 但慢 1.7 倍。形状缓存（mem_pattern）对速度没帮助，一律关
        so.enable_cpu_mem_arena = bool(bulk)
        so.enable_mem_pattern = False
        self._sess = ort.InferenceSession(str(model_file), so, providers=["CPUExecutionProvider"])
        self._tok = tok
        self._inputs = {i.name for i in self._sess.get_inputs()}
        self.max_tokens = int(max_tokens)
        self.dim = int(self._run(["测试"]).shape[1])

    def _run(self, texts: list[str]):
        np = self._np
        encs = self._tok.encode_batch(texts)
        feed = {"input_ids": np.array([e.ids for e in encs], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in encs], dtype=np.int64)}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
        out = self._sess.run(None, {k: v for k, v in feed.items() if k in self._inputs})[0]
        if out.ndim != 3:
            raise ValueError(f"模型输出形状不是 (批, 长, 维): {out.shape}")
        cls = out[:, 0, :].astype(np.float32)  # bge：[CLS] 池化
        norm = np.linalg.norm(cls, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return cls / norm

    def encode(self, texts: list[str], batch_size: int = 32):
        """texts -> (n, dim) float32，已 L2 归一化。按长度排序分批以减少 padding，返回时还原原顺序。"""
        np = self._np
        n = len(texts)
        out = np.empty((n, self.dim), dtype=np.float32)
        if n == 0:
            return out
        cap = self.max_tokens * 3  # 汉字约 1 字 1 token；先截字符再分词，免得对几万字的提示做无用功
        clipped = [(t or " ")[:cap] for t in texts]
        order = sorted(range(n), key=lambda i: len(clipped[i]))
        for s in range(0, n, batch_size):
            idx = order[s:s + batch_size]
            out[idx] = self._run([clipped[i] for i in idx])
        return out

    def encode_query(self, text: str):
        return self.encode([QUERY_PREFIX + (text or "")])[0]


# ---------------------------------------------------------------- 进程内单例

_lock = threading.Lock()
_inst: dict[bool, Embedder] = {}  # False: 查询用（常驻）；True: 补向量用（补完就释放）
_fail_until = 0.0
_last_used = 0.0
FAIL_RETRY_S = 120  # 加载失败后多久内不再尝试（模型文件不在不算失败，每次只是一次 stat）


def get(cfg=None, *, bulk: bool = False) -> Embedder | None:
    """返回常驻的嵌入器；模型不在、依赖缺失、加载失败返回 None（静默，只记一次日志）。"""
    global _fail_until, _last_used
    inst = _inst.get(bulk)
    if inst is not None:
        _last_used = time.time()
        return inst
    if not model_present():
        return None
    with _lock:
        inst = _inst.get(bulk)
        if inst is not None:
            return inst
        if time.time() < _fail_until:
            return None
        try:
            get_ = (lambda k, d: cfg.get(k, d)) if cfg is not None else (lambda k, d: d)
            t0 = time.time()
            inst = Embedder(model_dir(), max_tokens=int(get_("embed.max_tokens", 256)),
                            threads=int(get_("embed.threads", 4)), bulk=bulk)
            _log.info("嵌入模型已加载（%s，维度 %d，%.2f 秒）", "补向量用" if bulk else "查询用", inst.dim,
                      time.time() - t0)
            if inst.dim != EXPECTED_DIM:
                _log.warning("实测向量维度 %d，与文档写的 %d 不一致，以实测为准", inst.dim, EXPECTED_DIM)
        except Exception as e:  # ImportError、模型损坏、ort 出错：整条向量路关闭一阵
            _fail_until = time.time() + FAIL_RETRY_S
            _log.warning("嵌入模型加载失败，向量检索暂时关闭: %s: %s", type(e).__name__, str(e)[:200])
            return None
        _inst[bulk] = inst
        _last_used = time.time()
        return inst


def release(*, bulk: bool | None = None) -> bool:
    """释放模型。bulk=None 两个都放；返回是否真的释放了东西。（向量矩阵由 vectors.release 管）"""
    with _lock:
        keys = [False, True] if bulk is None else [bulk]
        had = any(k in _inst for k in keys)
        for k in keys:
            _inst.pop(k, None)
    return had


def maybe_unload(minutes: float) -> bool:
    """worker 心跳调：查询用的模型 minutes 分钟没人用就释放。"""
    if False in _inst and minutes > 0 and time.time() - _last_used >= minutes * 60:
        return release()
    return False
