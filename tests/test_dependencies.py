import threading

import pytest

from app.config import Config
from app.db import (
    DomainError,
    connect,
    get_db,
    save_dependencies,
    start_extract,
)


@pytest.fixture()
def auth(client):
    _req, resp = client.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    )
    assert resp.status == 200
    cookie = resp.cookies.get("flow_session")
    return {"Cookie": f"flow_session={cookie}"}


@pytest.fixture()
def hold_running():
    """把指定流置为抽取中并保持提交；DomainError 也不回滚该状态。"""
    conns = []

    def _hold(flow_id):
        c = connect(Config.database())
        start_extract(c, flow_id)
        c.commit()
        conns.append(c)
        return c

    yield _hold
    for c in conns:
        c.close()


def _flow_ids(client, headers):
    _req, resp = client.get("/api/flows", headers=headers)
    return [it["id"] for it in resp.json["items"]]


def _detail(client, headers, flow_id):
    _req, resp = client.get(f"/api/flows/{flow_id}", headers=headers)
    assert resp.status == 200
    return resp.json


def _save_deps(client, headers, downstream, upstreams, policy):
    return client.put(
        f"/api/flows/{downstream}/dependencies",
        headers=headers,
        json={"upstream_ids": upstreams, "policy": policy},
    )


# ---- 依赖图保存 -----------------------------------------------------------

def test_empty_dependency_graph_rejected(client, auth):
    ids = _flow_ids(client, auth)
    _req, resp = _save_deps(client, auth, ids[1], [], "作废")
    assert resp.status == 400
    assert "空依赖图不准保存" in resp.json["detail"]


def test_cycle_rejected(client, auth):
    ids = _flow_ids(client, auth)
    a, b = ids[0], ids[1]
    _req, r1 = _save_deps(client, auth, b, [a], "作废")
    assert r1.status == 200
    _req, r2 = _save_deps(client, auth, a, [b], "作废")
    assert r2.status == 400
    assert "成环" in r2.json["detail"]


def test_policy_pinned_same_day_locked(client, auth):
    ids = _flow_ids(client, auth)
    down, up_a, up_b = ids[0], ids[1], ids[2]
    _req, ok = _save_deps(client, auth, down, [up_a], "作废")
    assert ok.status == 200
    # 同内容重存允许（幂等）
    _req, same = _save_deps(client, auth, down, [up_a], "作废")
    assert same.status == 200
    # 当天换策略 → 拦住
    _req, change_policy = _save_deps(client, auth, down, [up_a], "等待舱")
    assert change_policy.status == 409
    assert "当天不许再换" in change_policy.json["detail"]
    # 当天换前置路径（依赖图内容）→ 拦住
    _req, change_up = _save_deps(client, auth, down, [up_a, up_b], "作废")
    assert change_up.status == 409
    assert "当天不许再换" in change_up.json["detail"]


def test_dependency_list_fields_next_to_run_detail(client, auth):
    ids = _flow_ids(client, auth)
    down, up = ids[1], ids[0]
    _req, resp = _save_deps(client, auth, down, [up], "作废")
    assert resp.status == 200
    detail = _detail(client, auth, down)
    dep = detail["dependency"]
    assert dep["policy"] == "作废"
    assert dep["ready"] is False
    assert dep["gate_label"] == "未齐件"
    assert dep["upstreams"][0]["id"] == up
    assert dep["upstreams"][0]["status"] == "待抽取"
    assert dep["upstreams"][0]["ready"] == 0
    assert dep["ready"] == 0
    # 列表须列出前置态、等待舱、中断数、策略名
    assert detail["interrupt_count"] == 0
    assert detail["in_hold"] is False
    assert "pinned_source_uri" in dep and "pinned_at" in dep
    assert detail["extract_entry_visible"] is True


# ---- 作废策略 -------------------------------------------------------------

def test_unready_upstream_abort_policy_interrupts(client, auth):
    ids = _flow_ids(client, auth)
    down, up = ids[1], ids[0]
    _save_deps(client, auth, down, [up], "作废")
    _req, resp = client.post(f"/api/flows/{down}/extract/start", headers=auth, json={})
    assert resp.status == 409
    assert "前置未齐件" in resp.json["detail"] and "整批作废" in resp.json["detail"]
    detail = _detail(client, auth, down)
    assert detail["status"] == "已中断"
    assert detail["interrupt_count"] == 1
    assert "前置未齐件" in detail["interrupt_reason"]
    assert detail["last_gate_reason"] == resp.json["detail"]


def test_ready_upstream_opens_gate(client, auth):
    ids = _flow_ids(client, auth)
    down, up = ids[1], ids[0]
    _save_deps(client, auth, down, [up], "作废")
    # 前置抽完即齐件
    _req, ex = client.post(f"/api/flows/{up}/extract", headers=auth, json={})
    assert ex.status == 200
    assert ex.json["status"] == "已抽取待落库"
    _req, start = client.post(f"/api/flows/{down}/extract/start", headers=auth, json={})
    assert start.status == 200
    assert start.json["status"] == "抽取中"
    _req, fin = client.post(f"/api/flows/{down}/extract/finish", headers=auth)
    assert fin.status == 200
    assert fin.json["status"] == "已抽取待落库"


# ---- 等待舱策略 -----------------------------------------------------------

def test_hold_policy_enters_hold_then_force_with_confirmation(client, auth):
    ids = _flow_ids(client, auth)
    down, up = ids[1], ids[0]
    _save_deps(client, auth, down, [up], "等待舱")
    _req, blocked = client.post(f"/api/flows/{down}/extract/start", headers=auth, json={})
    assert blocked.status == 409
    assert "等待舱" in blocked.json["detail"]
    detail = _detail(client, auth, down)
    assert detail["status"] == "等待舱"
    assert detail["in_hold"] is True
    # 在舱内普通抽取仍被拒
    _req, again = client.post(f"/api/flows/{down}/extract/start", headers=auth, json={})
    assert again.status == 409
    assert "等待舱" in again.json["detail"]
    # 强抽缺缘故 → 拒
    _req, no_reason = client.post(
        f"/api/flows/{down}/force-extract", headers=auth, json={"reason": "  "}
    )
    assert no_reason.status == 400
    assert "强抽缘故" in no_reason.json["detail"]
    # 人工确认强抽
    _req, forced = client.post(
        f"/api/flows/{down}/force-extract",
        headers=auth,
        json={"reason": "前置已人工核对可用，申请强抽"},
    )
    assert forced.status == 200
    assert forced.json["status"] == "已抽取待落库"
    assert forced.json["force_reason"] == "前置已人工核对可用，申请强抽"
    # 确认痕迹可单独核对
    _req, conf = client.get(f"/api/flows/{down}/confirmations", headers=auth)
    assert conf.status == 200
    items = conf.json["items"]
    assert len(items) == 1
    assert items[0]["reason"] == "前置已人工核对可用，申请强抽"
    assert items[0]["confirmed_by"] == "admin"
    assert items[0]["confirmed_at"]
    assert "目录扫描流" in items[0]["upstream_snapshot"]


def test_force_extract_blocked_while_upstream_running(client, auth, hold_running):
    ids = _flow_ids(client, auth)
    down, up = ids[1], ids[0]
    _save_deps(client, auth, down, [up], "等待舱")
    client.post(f"/api/flows/{down}/extract/start", headers=auth, json={})
    hold_running(up)
    _req, resp = client.post(
        f"/api/flows/{down}/force-extract",
        headers=auth,
        json={"reason": "想强抽"},
    )
    assert resp.status == 409
    assert "前置仍在抽取" in resp.json["detail"]


# ---- 入口可见性 -----------------------------------------------------------

def test_running_upstream_hides_downstream_entry(client, auth, hold_running):
    ids = _flow_ids(client, auth)
    down, up = ids[1], ids[0]
    _save_deps(client, auth, down, [up], "作废")
    hold_running(up)
    detail = _detail(client, auth, down)
    assert detail["extract_entry_visible"] == 0
    # 后台同样强制拦截
    _req, resp = client.post(f"/api/flows/{down}/extract/start", headers=auth, json={})
    assert resp.status == 409
    assert "前置未齐件" in resp.json["detail"]


# ---- 路径漂移 -------------------------------------------------------------

def test_path_drift_blocked_with_chinese_reason(client, auth):
    ids = _flow_ids(client, auth)
    down, up = ids[1], ids[0]
    _save_deps(client, auth, down, [up], "作废")
    _req, upd = client.put(
        f"/api/flows/{down}/source-uri",
        headers=auth,
        json={"source_uri": "https://mock.local/drifted-elsewhere"},
    )
    assert upd.status == 200
    _req, resp = client.post(f"/api/flows/{down}/extract/start", headers=auth, json={})
    assert resp.status == 409
    reason = resp.json["detail"]
    assert "依赖路径漂移" in reason
    assert "drifted-elsewhere" in reason
    detail = _detail(client, auth, down)
    assert detail["last_gate_reason"] == reason
    # 走整段抽取接口同样被拦
    _req, resp2 = client.post(f"/api/flows/{down}/extract", headers=auth, json={})
    assert resp2.status == 409
    assert "依赖路径漂移" in resp2.json["detail"]


# ---- 并发争用 -------------------------------------------------------------

def test_concurrent_extract_only_first_wins(client):
    with get_db(Config.database()) as conn:
        rows = conn.execute("SELECT id FROM flows ORDER BY id").fetchall()
        target = rows[2]["id"]

    barrier = threading.Barrier(2)
    results = {}

    def worker(name):
        db = connect(Config.database())
        try:
            barrier.wait()
            start_extract(db, target)
            db.commit()
            results[name] = "won"
        except DomainError as exc:
            results[name] = exc.reason
        finally:
            db.close()

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    outcomes = list(results.values())
    assert outcomes.count("won") == 1
    lost = [m for m in outcomes if m != "won"][0]
    assert "争用败绩" in lost
    with get_db(Config.database()) as conn:
        row = conn.execute(
            "SELECT status, contest_lost_count FROM flows WHERE id=?", (target,)
        ).fetchone()
        assert row["status"] == "抽取中"
        assert row["contest_lost_count"] == 1


def test_sequential_contest_lost_counter(client, auth, hold_running):
    ids = _flow_ids(client, auth)
    target = ids[3]
    hold_running(target)
    _req, resp = client.post(f"/api/flows/{target}/extract/start", headers=auth, json={})
    assert resp.status == 409
    assert "争用败绩" in resp.json["detail"]
    detail = _detail(client, auth, target)
    assert detail["contest_lost_count"] == 1
    assert detail["extract_entry_visible"] == 0
