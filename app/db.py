import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.config import Config

# ---- 领域常量 -------------------------------------------------------------

STATUS_PENDING = "待抽取"
STATUS_RUNNING = "抽取中"
STATUS_EXTRACTED = "已抽取待落库"
STATUS_LANDED = "已落库"
STATUS_FAILED = "抽取失败"
STATUS_INTERRUPTED = "已中断"
STATUS_HOLD = "等待舱"

# 允许发起抽取的状态（等待舱必须走人工确认强抽）
EXTRACTABLE_STATUSES = (STATUS_PENDING, STATUS_FAILED, STATUS_INTERRUPTED)
# 前置齐件认可的状态
UPSTREAM_READY_STATUSES = (STATUS_EXTRACTED, STATUS_LANDED)

POLICY_ABORT = "作废"
POLICY_HOLD = "等待舱"
POLICIES = (POLICY_ABORT, POLICY_HOLD)

SEEDS = [
    ("目录扫描流", "scan_flow", "待抽取", "https://mock.local/scan"),
    ("列表翻页流", "page_flow", "待抽取", "https://mock.local/list"),
    ("附件下载流", "file_flow", "待抽取", "file://drops/pack"),
    ("变更监听流", "watch_flow", "待抽取", "https://mock.local/watch"),
]

_FLOW_NEW_COLUMNS = [
    ("interrupt_reason", "TEXT"),
    ("interrupt_count", "INTEGER NOT NULL DEFAULT 0"),
    ("force_reason", "TEXT"),
    ("force_confirmed_by", "TEXT"),
    ("force_confirmed_at", "TEXT"),
    ("contest_lost_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_gate_reason", "TEXT"),
    ("lock_owner", "TEXT"),
    ("lock_at", "TEXT"),
]


class DomainError(Exception):
    """业务闸口错误，message 一律为可直接回写页面的中文原因。"""

    def __init__(self, reason: str, status: int = 409):
        super().__init__(reason)
        self.reason = reason
        self.status = status


# ---- 连接与建表 -----------------------------------------------------------

def connect(db_path=None):
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db(db_path=None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path=None):
    with get_db(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS flows (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              code TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL,
              source_uri TEXT NOT NULL,
              extracted INTEGER NOT NULL DEFAULT 0,
              landed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS flow_dependency_config (
              downstream_id INTEGER PRIMARY KEY REFERENCES flows(id),
              policy TEXT NOT NULL,
              pinned_source_uri TEXT NOT NULL,
              pinned_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS flow_dependencies (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              downstream_id INTEGER NOT NULL REFERENCES flows(id),
              upstream_id INTEGER NOT NULL REFERENCES flows(id),
              UNIQUE(downstream_id, upstream_id)
            );
            CREATE TABLE IF NOT EXISTS force_confirmations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              flow_id INTEGER NOT NULL REFERENCES flows(id),
              reason TEXT NOT NULL,
              confirmed_by TEXT NOT NULL,
              confirmed_at TEXT NOT NULL,
              upstream_snapshot TEXT NOT NULL
            );
            """
        )
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(flows)")}
        for name, decl in _FLOW_NEW_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE flows ADD COLUMN {name} {decl}")
        if conn.execute("SELECT COUNT(*) c FROM flows").fetchone()["c"] == 0:
            conn.executemany(
                "INSERT INTO flows (title, code, status, source_uri) VALUES (?,?,?,?)",
                SEEDS,
            )


# ---- 基础查询 -------------------------------------------------------------

def list_flows(conn):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, title, code, status, source_uri, extracted, landed "
            "FROM flows ORDER BY id"
        )
    ]


def get_flow(conn, flow_id: int):
    row = conn.execute(
        """
        SELECT id, title, code, status, source_uri, extracted, landed,
               interrupt_reason, interrupt_count, force_reason,
               force_confirmed_by, force_confirmed_at, contest_lost_count,
               last_gate_reason, lock_owner, lock_at
        FROM flows WHERE id=?
        """,
        (flow_id,),
    ).fetchone()
    return dict(row) if row else None


def _dependency_block(conn, flow: dict):
    config = conn.execute(
        "SELECT policy, pinned_source_uri, pinned_at FROM flow_dependency_config "
        "WHERE downstream_id=?",
        (flow["id"],),
    ).fetchone()
    if not config:
        return None
    upstreams = [
        dict(r)
        for r in conn.execute(
            """
            SELECT f.id, f.title, f.code, f.status,
                   f.status IN (?, ?) AS ready
            FROM flow_dependencies d
            JOIN flows f ON f.id = d.upstream_id
            WHERE d.downstream_id=?
            ORDER BY f.id
            """,
            (*UPSTREAM_READY_STATUSES, flow["id"]),
        )
    ]
    missing = [u for u in upstreams if not u["ready"]]
    running = [u for u in upstreams if u["status"] == STATUS_RUNNING]
    return {
        "policy": config["policy"],
        "pinned_source_uri": config["pinned_source_uri"],
        "pinned_at": config["pinned_at"],
        "ready": not missing,
        "gate_label": "齐件" if not missing else "未齐件",
        "upstreams": upstreams,
        "missing_upstreams": missing,
        "running_upstreams": running,
    }


def get_flow_detail(conn, flow_id: int):
    flow = get_flow(conn, flow_id)
    if not flow:
        return None
    dep = _dependency_block(conn, flow)
    flow["dependency"] = dep
    # 前置仍在抽取（或自身在抽）时，下游抽取入口保持看不见
    entry_visible = flow["status"] != STATUS_RUNNING
    if dep and dep["running_upstreams"]:
        entry_visible = False
    flow["extract_entry_visible"] = entry_visible
    flow["in_hold"] = flow["status"] == STATUS_HOLD
    flow["interrupted"] = flow["status"] == STATUS_INTERRUPTED
    return flow


# ---- 依赖图保存（同轮钉死策略） -------------------------------------------

def _would_cycle(conn, downstream_id: int, upstream_ids: list[int]) -> bool:
    # 新图：本次下游的边用新集合，其余下游沿用已存边
    adjacency: dict[int, list[int]] = {}
    for r in conn.execute("SELECT downstream_id, upstream_id FROM flow_dependencies"):
        adjacency.setdefault(r["downstream_id"], []).append(r["upstream_id"])
    adjacency[downstream_id] = list(upstream_ids)
    # 从任一前置出发，若能沿边回到下游，则成环
    stack = list(upstream_ids)
    seen = set()
    while stack:
        node = stack.pop()
        if node == downstream_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, []))
    return False


def save_dependencies(conn, downstream_id: int, upstream_ids, policy: str, now=None):
    now = now or datetime.now()
    flow = get_flow(conn, downstream_id)
    if not flow:
        raise DomainError("流不存在", 404)

    ids = [int(x) for x in (upstream_ids or [])]
    if not ids:
        raise DomainError("空依赖图不准保存：下游至少挂一个前置任务编号", 400)
    if downstream_id in ids:
        raise DomainError("不能将自身挂为前置任务", 400)
    if len(set(ids)) != len(ids):
        raise DomainError("前置任务编号重复，请去重后再保存", 400)
    if policy not in POLICIES:
        raise DomainError(f"依赖策略非法，只允许「{POLICY_ABORT}」或「{POLICY_HOLD}」", 400)
    placeholders = ",".join("?" * len(ids))
    found = {
        r["id"]
        for r in conn.execute(
            f"SELECT id FROM flows WHERE id IN ({placeholders})", ids
        )
    }
    unknown = [i for i in ids if i not in found]
    if unknown:
        raise DomainError(f"前置任务不存在：编号 {unknown}", 400)
    if _would_cycle(conn, downstream_id, ids):
        raise DomainError("依赖图成环，前置链条会回到本任务，不准保存", 400)

    existing = conn.execute(
        "SELECT policy, pinned_source_uri, pinned_at FROM flow_dependency_config "
        "WHERE downstream_id=?",
        (downstream_id,),
    ).fetchone()
    today = now.strftime("%Y-%m-%d")
    if existing and existing["pinned_at"][:10] == today:
        old_ids = {
            r["upstream_id"]
            for r in conn.execute(
                "SELECT upstream_id FROM flow_dependencies WHERE downstream_id=?",
                (downstream_id,),
            )
        }
        changed = (
            existing["policy"] != policy
            or set(ids) != old_ids
            or existing["pinned_source_uri"] != flow["source_uri"]
        )
        if changed:
            raise DomainError(
                f"依赖策略已于今日钉死（{existing['pinned_at']}），当天不许再换",
                409,
            )
    conn.execute(
        "DELETE FROM flow_dependencies WHERE downstream_id=?", (downstream_id,)
    )
    conn.executemany(
        "INSERT INTO flow_dependencies (downstream_id, upstream_id) VALUES (?,?)",
        [(downstream_id, uid) for uid in ids],
    )
    conn.execute(
        """
        INSERT INTO flow_dependency_config
          (downstream_id, policy, pinned_source_uri, pinned_at)
        VALUES (?,?,?,?)
        ON CONFLICT(downstream_id) DO UPDATE SET
          policy=excluded.policy,
          pinned_source_uri=excluded.pinned_source_uri,
          pinned_at=excluded.pinned_at
        """,
        (downstream_id, policy, flow["source_uri"], now.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    return get_flow_detail(conn, downstream_id)


# ---- 抽取闸口 -------------------------------------------------------------

def _record_gate(conn, flow_id: int, reason: str):
    # 独立自动提交连接：随后主事务即便因 DomainError 回滚，
    # 闸口中文原因也已落库，可在页面核对
    note_conn = connect()
    try:
        note_conn.execute(
            "UPDATE flows SET last_gate_reason=? WHERE id=?", (reason, flow_id)
        )
        note_conn.commit()
    finally:
        note_conn.close()


def _drift_reason(flow: dict, dep: dict, override_uri: str | None):
    current = override_uri if override_uri is not None else flow["source_uri"]
    if current != dep["pinned_source_uri"]:
        return (
            "依赖路径漂移：当前路径「%s」与钉死路径「%s」不一致，已拦截开抽"
            % (current, dep["pinned_source_uri"])
        )
    return None


def _unready_names(dep: dict):
    return "、".join(
        f"{u['title']}（{u['status']}）" for u in dep["missing_upstreams"]
    )


def start_extract(conn, flow_id: int, source_uri: str | None = None, now=None):
    now = now or datetime.now()
    flow = get_flow(conn, flow_id)
    if not flow:
        raise DomainError("流不存在", 404)
    dep = _dependency_block(conn, flow)

    # 路径漂移闸：任何开抽方式都先比对钉死路径
    if dep:
        drift = _drift_reason(flow, dep, source_uri)
        if drift:
            _record_gate(conn, flow_id, drift)
            raise DomainError(drift, 409)

    if flow["status"] == STATUS_HOLD:
        reason = "任务处于等待舱，须经人工确认并填写强抽缘故后才允许强行开抽"
        _record_gate(conn, flow_id, reason)
        raise DomainError(reason, 409)

    # 抢注与策略处置用独立自动提交连接：调用方即便捕获 DomainError 回滚，
    # 抽取锁、中断态、等待舱态与闸口原因也都已落库
    tx = connect()
    try:
        # 原子抢注：同刻并抽只留先手，败者记下游争用败绩
        ts = now.strftime("%Y-%m-%dT%H:%M:%S")
        cur = tx.execute(
            "UPDATE flows SET status=?, lock_owner=?, lock_at=? "
            "WHERE id=? AND status!=?",
            (STATUS_RUNNING, "api", ts, flow_id, STATUS_RUNNING),
        )
        if cur.rowcount == 0:
            tx.execute(
                "UPDATE flows SET contest_lost_count=contest_lost_count+1 WHERE id=?",
                (flow_id,),
            )
            reason = "下游争用败绩：该任务已被先手抢占抽取，本次开抽未获先手"
            tx.execute(
                "UPDATE flows SET last_gate_reason=? WHERE id=?", (reason, flow_id)
            )
            tx.commit()
            raise DomainError(reason, 409)

        if flow["status"] not in EXTRACTABLE_STATUSES:
            tx.rollback()
            reason = f"当前状态「{flow['status']}」不允许开抽"
            _record_gate(conn, flow_id, reason)
            raise DomainError(reason, 409)

        # 前置齐件闸：抢注成功后再核，未齐按钉死策略处置
        if dep and not dep["ready"]:
            names = _unready_names(dep)
            if dep["policy"] == POLICY_ABORT:
                reason = (
                    f"前置未齐件，依赖策略「整批作废」：{names} 尚未齐备，"
                    "下游任务整批作废并中断"
                )
                tx.execute(
                    "UPDATE flows SET status=?, interrupt_reason=?, "
                    "interrupt_count=interrupt_count+1, last_gate_reason=?, "
                    "lock_owner=NULL, lock_at=NULL WHERE id=?",
                    (STATUS_INTERRUPTED, reason, reason, flow_id),
                )
                tx.commit()
                raise DomainError(reason, 409)
            reason = (
                f"前置未齐件，依赖策略「等待舱」：{names} 尚未齐备，"
                "下游转入等待舱，待人工确认后强行开抽"
            )
            tx.execute(
                "UPDATE flows SET status=?, interrupt_reason=?, last_gate_reason=?, "
                "lock_owner=NULL, lock_at=NULL WHERE id=?",
                (STATUS_HOLD, reason, reason, flow_id),
            )
            tx.commit()
            raise DomainError(reason, 409)
        tx.commit()
    except Exception:
        tx.rollback()
        raise
    finally:
        tx.close()
    return get_flow(conn, flow_id)


def finish_extract(conn, flow_id: int):
    flow = get_flow(conn, flow_id)
    if not flow:
        raise DomainError("流不存在", 404)
    if flow["status"] != STATUS_RUNNING:
        raise DomainError(f"当前状态「{flow['status']}」不在抽取中，无法收尾", 409)
    n = 15 + (flow_id * 4) % 10
    conn.execute(
        "UPDATE flows SET status=?, extracted=?, landed=0, "
        "lock_owner=NULL, lock_at=NULL WHERE id=?",
        (STATUS_EXTRACTED, n, flow_id),
    )
    return get_flow(conn, flow_id)


def run_extract(conn, flow_id: int, source_uri: str | None = None, now=None):
    start_extract(conn, flow_id, source_uri=source_uri, now=now)
    return finish_extract(conn, flow_id)


def force_extract(conn, flow_id: int, reason: str, user: str, now=None):
    now = now or datetime.now()
    flow = get_flow(conn, flow_id)
    if not flow:
        raise DomainError("流不存在", 404)
    if flow["status"] != STATUS_HOLD:
        raise DomainError("当前不在等待舱，无需人工确认强行开抽", 409)
    dep = _dependency_block(conn, flow)
    if not dep:
        raise DomainError("该任务没有前置依赖图，不存在等待舱确认", 409)

    drift = _drift_reason(flow, dep, None)
    if drift:
        _record_gate(conn, flow_id, drift)
        raise DomainError(drift, 409)
    if dep["running_upstreams"]:
        reason_block = "前置仍在抽取中，下游入口关闭，暂不能强行开抽"
        _record_gate(conn, flow_id, reason_block)
        raise DomainError(reason_block, 409)

    text = (reason or "").strip()
    if not text:
        raise DomainError("强行开抽必须填写强抽缘故，留痕待核", 400)

    snapshot = json.dumps(
        [
            {"id": u["id"], "title": u["title"], "status": u["status"]}
            for u in dep["upstreams"]
        ],
        ensure_ascii=False,
    )
    ts = now.strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "INSERT INTO force_confirmations "
        "(flow_id, reason, confirmed_by, confirmed_at, upstream_snapshot) "
        "VALUES (?,?,?,?,?)",
        (flow_id, text, user, ts, snapshot),
    )
    conn.execute(
        "UPDATE flows SET force_reason=?, force_confirmed_by=?, "
        "force_confirmed_at=?, last_gate_reason=NULL WHERE id=?",
        (text, user, ts, flow_id),
    )
    # 人工确认后放行：置抽取中并立即收尾
    conn.execute(
        "UPDATE flows SET status=?, lock_owner=?, lock_at=? WHERE id=?",
        (STATUS_RUNNING, user, ts, flow_id),
    )
    return finish_extract(conn, flow_id)


def list_confirmations(conn, flow_id: int):
    flow = get_flow(conn, flow_id)
    if not flow:
        raise DomainError("流不存在", 404)
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, flow_id, reason, confirmed_by, confirmed_at, upstream_snapshot "
            "FROM force_confirmations WHERE flow_id=? ORDER BY id DESC",
            (flow_id,),
        )
    ]


def update_source_uri(conn, flow_id: int, source_uri: str):
    flow = get_flow(conn, flow_id)
    if not flow:
        raise DomainError("流不存在", 404)
    uri = (source_uri or "").strip()
    if not uri:
        raise DomainError("路径不许为空", 400)
    conn.execute("UPDATE flows SET source_uri=? WHERE id=?", (uri, flow_id))
    return get_flow_detail(conn, flow_id)


def run_land(conn, flow_id: int):
    item = get_flow(conn, flow_id)
    if not item:
        return None
    if item["status"] == STATUS_EXTRACTED:
        conn.execute(
            "UPDATE flows SET status=?, landed=? WHERE id=?",
            (STATUS_LANDED, item["extracted"], flow_id),
        )
    return get_flow(conn, flow_id)
