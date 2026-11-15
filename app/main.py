from pathlib import Path

from itsdangerous import BadSignature, URLSafeSerializer
from sanic import Request, Sanic, json, redirect
from sanic.response import file

from app.config import Config
from app.db import (
    DomainError,
    finish_extract,
    force_extract,
    get_db,
    get_flow_detail,
    init_db,
    list_confirmations,
    list_flows,
    run_extract,
    run_land,
    save_dependencies,
    start_extract,
    update_source_uri,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
ser = URLSafeSerializer(Config.SECRET, salt="cc-flow-session")

app = Sanic("cc-flow-lab")
app.static("/static", str(STATIC), name="static")


def _user(request: Request) -> str | None:
    raw = request.cookies.get("flow_session")
    if not raw:
        return None
    try:
        data = ser.loads(raw)
        u = data.get("user") if isinstance(data, dict) else None
        return u if u == Config.ADMIN_USER else None
    except BadSignature:
        return None


def _unauthorized():
    return json({"detail": "未登录"}, status=401)


def _body(request: Request) -> dict:
    return request.json or {}


@app.listener("before_server_start")
async def _boot(_app, _loop):
    init_db()


@app.get("/")
async def root(request: Request):
    return redirect("/app/" if _user(request) else "/login")


@app.get("/login")
async def login_page(request: Request):
    return await file(STATIC / "login.html")


@app.get("/app/", name="spa_root")
async def spa_root(request: Request):
    if not _user(request):
        return redirect("/login")
    return await file(STATIC / "index.html")


@app.get("/app/<path:path>", name="spa_path")
async def spa_path(request: Request, path: str = ""):
    if not _user(request):
        return redirect("/login")
    return await file(STATIC / "index.html")


@app.post("/api/login")
async def api_login(request: Request):
    body = _body(request)
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json({"ok": False, "error": "账号或密码错误"}, status=400)
    resp = json({"ok": True, "user": user})
    resp.add_cookie(
        "flow_session",
        ser.dumps({"user": user}),
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    resp = json({"ok": True})
    resp.delete_cookie("flow_session", path="/")
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    user = _user(request)
    if not user:
        return _unauthorized()
    return json({"user": user, "lab": Config.LAB_NAME, "domain": "数据工程"})


@app.get("/api/flows")
async def api_flows(request: Request):
    if not _user(request):
        return _unauthorized()
    with get_db() as conn:
        return json({"items": list_flows(conn)})


@app.get("/api/flows/<flow_id:int>")
async def api_flow(request: Request, flow_id: int):
    if not _user(request):
        return _unauthorized()
    with get_db() as conn:
        item = get_flow_detail(conn, flow_id)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json(item)


@app.put("/api/flows/<flow_id:int>/source-uri")
async def api_source_uri(request: Request, flow_id: int):
    user = _user(request)
    if not user:
        return _unauthorized()
    body = _body(request)
    try:
        with get_db() as conn:
            item = update_source_uri(conn, flow_id, str(body.get("source_uri", "")))
    except DomainError as exc:
        return json({"detail": exc.reason}, status=exc.status)
    return json(item)


@app.put("/api/flows/<flow_id:int>/dependencies")
async def api_save_dependencies(request: Request, flow_id: int):
    if not _user(request):
        return _unauthorized()
    body = _body(request)
    upstream_ids = body.get("upstream_ids") or []
    policy = str(body.get("policy", ""))
    try:
        with get_db() as conn:
            item = save_dependencies(conn, flow_id, upstream_ids, policy)
    except DomainError as exc:
        return json({"detail": exc.reason}, status=exc.status)
    return json(item)


@app.get("/api/flows/<flow_id:int>/confirmations")
async def api_confirmations(request: Request, flow_id: int):
    if not _user(request):
        return _unauthorized()
    try:
        with get_db() as conn:
            items = list_confirmations(conn, flow_id)
    except DomainError as exc:
        return json({"detail": exc.reason}, status=exc.status)
    return json({"items": items})


@app.post("/api/flows/<flow_id:int>/extract/start")
async def api_extract_start(request: Request, flow_id: int):
    if not _user(request):
        return _unauthorized()
    body = _body(request)
    source_uri = body.get("source_uri")
    try:
        with get_db() as conn:
            item = start_extract(conn, flow_id, source_uri=source_uri)
    except DomainError as exc:
        return json({"detail": exc.reason}, status=exc.status)
    return json(item)


@app.post("/api/flows/<flow_id:int>/extract/finish")
async def api_extract_finish(request: Request, flow_id: int):
    if not _user(request):
        return _unauthorized()
    try:
        with get_db() as conn:
            item = finish_extract(conn, flow_id)
    except DomainError as exc:
        return json({"detail": exc.reason}, status=exc.status)
    return json(item)


@app.post("/api/flows/<flow_id:int>/extract")
async def api_extract(request: Request, flow_id: int):
    if not _user(request):
        return _unauthorized()
    body = _body(request)
    source_uri = body.get("source_uri")
    try:
        with get_db() as conn:
            item = run_extract(conn, flow_id, source_uri=source_uri)
    except DomainError as exc:
        return json({"detail": exc.reason}, status=exc.status)
    return json(item)


@app.post("/api/flows/<flow_id:int>/force-extract")
async def api_force_extract(request: Request, flow_id: int):
    user = _user(request)
    if not user:
        return _unauthorized()
    body = _body(request)
    reason = str(body.get("reason", ""))
    try:
        with get_db() as conn:
            item = force_extract(conn, flow_id, reason, user)
    except DomainError as exc:
        return json({"detail": exc.reason}, status=exc.status)
    return json(item)


@app.post("/api/flows/<flow_id:int>/land")
async def api_land(request: Request, flow_id: int):
    if not _user(request):
        return _unauthorized()
    with get_db() as conn:
        item = run_land(conn, flow_id)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json(item)
