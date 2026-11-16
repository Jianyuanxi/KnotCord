import pytest

from knotcord.core import db, lease
from knotcord.core.lease import Lease, LeaseLost, holder_info


@pytest.fixture
def conns(trace_home):
    a, b = db.connect(), db.connect()
    yield a, b
    a.close()
    b.close()


def test_exclusive_then_takeover_after_expiry(conns, monkeypatch):
    ca, cb = conns
    clock = [1000.0]
    monkeypatch.setattr(lease, "_now", lambda: clock[0])
    A = Lease(ca, owner="A", ttl_s=90)
    B = Lease(cb, owner="B", ttl_s=90)
    assert A.try_acquire() is True
    assert B.try_acquire() is False
    assert A.is_mine() and not B.is_mine()
    info = holder_info(cb)
    assert info["owner"] == "A" and info["expires"] == 1090.0 and info["expired"] is False
    # 自己再抢 = 续
    clock[0] = 1050.0
    assert A.try_acquire() is True
    assert A.renew() is True
    assert holder_info(cb)["expires"] == 1140.0
    # 过期后 B 能抢
    clock[0] = 1141.0
    assert holder_info(cb)["expired"] is True
    assert B.try_acquire() is True
    assert holder_info(ca)["owner"] == "B"
    # 此时 A 的 renew 返回 False，落库断言抛异常
    assert A.renew() is False
    assert A.is_mine() is False
    ca.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(LeaseLost):
            A.assert_mine_in_tx()
    finally:
        ca.execute("ROLLBACK")
    A.release()  # 不是自己的，release 不能动别人的
    assert holder_info(ca)["owner"] == "B"


def test_assert_ok_for_holder_and_release(conns):
    ca, cb = conns
    A, B = Lease(ca, owner="A"), Lease(cb, owner="B")
    assert A.try_acquire()
    ca.execute("BEGIN IMMEDIATE")
    A.assert_mine_in_tx()
    ca.execute("ROLLBACK")
    A.release()
    assert holder_info(ca) is None
    assert B.try_acquire() is True


def test_default_owner_unique_and_shape(conns):
    ca, _ = conns
    o1, o2 = Lease(ca).owner, Lease(ca).owner
    assert o1 != o2
    pid, start, rnd = o1.split(":")
    assert pid.isdigit() and start.isdigit() and rnd


def test_holder_info_none_when_empty(conns):
    assert holder_info(conns[0]) is None


def test_corrupt_lease_row_is_overwritten(conns):
    ca, _ = conns
    db.kv_set(ca, "lease", "not json")
    assert holder_info(ca) is None
    A = Lease(ca, owner="A")
    assert A.try_acquire() is True
