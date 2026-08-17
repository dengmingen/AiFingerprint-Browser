"""指纹浏览器工作台 —— 本地 API 服务（Phase 3）。

响应格式与 AdsPower Local API 类似：HTTP 200 + {"code": 0, "msg": "success", "data": ...}，
code 非 0 表示业务失败，便于自动化脚本统一处理。

认证模型：
- API Key 关闭时：本地单用户模式，所有请求以管理员身份执行
- API Key 开启时：X-API-Key 必须命中某个启用的成员密钥；
  admin 可见/操作全部环境与任务，operator 仅能操作自己创建的环境/任务
- 同步端点（/api/sync/*）由 X-Sync-Token 保护，与成员体系独立
所有变更操作写入审计日志（含操作成员）。
"""
import asyncio
import logging
import secrets as _secrets_mod
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, matrix, pairing, pw_server, security, sync, transfer
from .kernels import camoufox_kernel, chromium_kernel, fp_chromium_kernel
from .config import (APP_NAME, BASE_DIR, DATA_DIR, DETECT_LINKS, PHASE,
                     PROFILE_ROOT, ROLE_ADMIN, ROLE_OPERATOR, VERSION)
from .fingerprint_engine import create_fingerprint, summarize
from .launcher import LaunchError, LaunchManager
from .models import (
    BatchStartRequest,
    BrowserStartRequest,
    ClickRequest,
    EvaluateRequest,
    ExtractRequest,
    GetHtmlRequest,
    HoverRequest,
    IdsRequest,
    MemberCreate,
    NavigateRequest,
    PlaywrightServerRequest,
    PressRequest,
    ProfileBatchCreate,
    ProfileCreate,
    ProfileExportData,
    ProfileUpdate,
    ProxyTestRequest,
    ScheduleCreate,
    ScheduleUpdate,
    ScreenshotRequest,
    ScrollRequest,
    SelectRequest,
    SettingsUpdate,
    TaskCreate,
    TaskRunRequest,
    TaskUpdate,
    TypeRequest,
    WaitRequest,
    WaitForRequest,
)
from .proxy_test import test_proxy
from .readiness import run_readiness
from .risk_presets import PRESET_DOC
from .scheduler import Scheduler
from .task_engine import TaskEngine, validate_steps

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fpworkbench")

manager = LaunchManager()
task_engine = TaskEngine(manager)
scheduler = Scheduler(task_engine)
_kernel_health: dict[str, Any] = {"status": "ok", "note": None}


async def _check_kernel_health():
    """后台拉取 Camoufox GitHub README 检查维护状态；失败不阻塞。"""
    global _kernel_health
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://raw.githubusercontent.com/daijro/camoufox/main/README.md")
            r.raise_for_status()
            body = r.text[:8000]
        # 关键信号：cloverlabs 分支推荐
        if "cloverlabs" in body.lower():
            _kernel_health = {
                "status": "notice",
                "note": "Camoufox 官方推荐 cloverlabs 分支（紧跟 Firefox 更新）。"
                        "切换：pip install cloverlabs-camoufox && camoufox set prerelease"}
        elif "maintenance" in body.lower() and "issue" in body.lower():
            _kernel_health = {"status": "warning",
                              "note": "Camoufox 可能在维护中，检测率可能升高。"
                                      "替代：使用 fp-chromium 内核或 cloverlabs 分支。"}
        else:
            _kernel_health = {"status": "ok", "note": None}
    except Exception:
        pass  # 网络不通不告警（离线环境正常）


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db.ensure_default_member()
    scheduler.start()
    asyncio.create_task(_check_kernel_health())
    log.info("%s v%s (Phase %s) 已启动", APP_NAME, VERSION, PHASE)
    yield
    await scheduler.shutdown()
    await task_engine.shutdown()
    await manager.stop_all()
    await pw_server.stop_all()


app = FastAPI(title=f"{APP_NAME} Local API", version=VERSION, lifespan=lifespan)

# CORS：默认仅放行本机页面与浏览器插件（chrome-extension://）来源；
# 远程使用请在安全边界内自行收紧/放宽
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?|chrome-extension://[a-z0-9]+)$",
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# 认证开启时不对外暴露接口文档（防止接口枚举）
_DOCS_PATHS = ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json")


class ApiError(Exception):
    def __init__(self, code: int, msg: str):
        self.code = code
        self.msg = msg


def ok(data: Any = None) -> dict:
    return {"code": 0, "msg": "success", "data": data}


def audit(action: str, target: str = "", detail: str = "", result: str = "ok",
          member: Optional[dict] = None) -> None:
    if member and member.get("id") != "admin":
        detail = f"[{member.get('name')}] {detail}".strip()
    try:
        db.audit_log(action, target, detail, result)
    except Exception:
        log.exception("审计日志写入失败")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    settings = security.load_settings()

    # 同步服务器端点：独立 token 保护
    if path.startswith("/api/sync/"):
        if not settings.get("sync_server_enabled"):
            return JSONResponse({"code": 40300, "msg": "同步服务器未开启", "data": None}, status_code=403)
        if not _secrets_mod.compare_digest(request.headers.get("x-sync-token") or "",
                                           settings.get("sync_token") or ""):
            db.audit_log("sync.denied", path, "同步令牌校验失败", "denied")
            return JSONResponse({"code": 40300, "msg": "同步令牌无效", "data": None}, status_code=403)
        request.state.member = {"id": "admin", "name": "同步节点", "role": ROLE_ADMIN}
        return await call_next(request)

    # 认证开启时隐藏 Swagger/OpenAPI（避免接口枚举；本机调试可关闭认证或带 Key 访问）
    if path in _DOCS_PATHS and settings.get("api_key_enabled"):
        member = db.get_member_by_key(request.headers.get("x-api-key") or "")
        if not member:
            return JSONResponse({"code": 40100, "msg": "文档未公开（需 X-API-Key）", "data": None},
                                status_code=401)

    if path.startswith("/api/") and path not in ("/api/v1/auth/verify", "/api/v1/pair/exchange"):
        if settings.get("api_key_enabled"):
            member = db.get_member_by_key(request.headers.get("x-api-key") or "")
            if not member:
                db.audit_log("auth.denied", path, "API Key 校验失败", "denied")
                return JSONResponse(
                    {"code": 40100, "msg": "API Key 无效或缺失（X-API-Key 头）", "data": None},
                    status_code=401,
                )
            request.state.member = member
        else:
            # 本地单用户模式：以管理员身份执行
            request.state.member = {"id": "admin", "name": "管理员", "role": ROLE_ADMIN}
    return await call_next(request)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    # 业务码为标准 HTTP 状态码区间时，同步设置 HTTP 状态（REST 客户端友好）
    status = exc.code if 400 <= exc.code < 600 else 200
    return JSONResponse({"code": exc.code, "msg": exc.msg, "data": None}, status_code=status)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("未处理异常: %s", exc)
    return JSONResponse({"code": -1, "msg": f"{type(exc).__name__}: {exc}", "data": None})


# ---------------------------------------------------------------- 权限工具

def member_of(request: Request) -> dict:
    return getattr(request.state, "member", {"id": "admin", "name": "管理员", "role": ROLE_ADMIN})


def is_admin(member: dict) -> bool:
    return member.get("role") == ROLE_ADMIN


def require_admin(request: Request) -> dict:
    member = member_of(request)
    if not is_admin(member):
        raise ApiError(403, "该操作需要管理员权限")
    return member


def scoped_profiles(request: Request) -> list[dict]:
    member = member_of(request)
    if is_admin(member):
        return db.list_profiles()
    return db.list_profiles(owner=member["id"])


def require_profile_access(request: Request, profile: dict) -> dict:
    member = member_of(request)
    if not is_admin(member) and profile.get("owner") != member["id"]:
        raise ApiError(403, "无权访问该环境（仅创建者或管理员）")
    return profile


def _serialize_profile(p: dict, full: bool = False) -> dict:
    out = {
        "id": p["id"],
        "name": p["name"],
        "group_name": p["group_name"],
        "notes": p["notes"],
        "kernel": p["kernel"],
        "target_os": p["target_os"],
        "proxy": {**p["proxy"], "password": "******"} if p["proxy"] else None,
        "launch": p["launch"],
        "fingerprint_summary": summarize(p["fingerprint"]),
        "running": manager.is_running(p["id"]),
        "owner": p.get("owner", "admin"),
        "start_count": p.get("start_count", 0),
        "last_started_at": p.get("last_started_at"),
        "created_at": p["created_at"],
        "updated_at": p["updated_at"],
    }
    if full:
        out["fingerprint"] = p["fingerprint"]
        if p["proxy"]:
            out["proxy_full"] = p["proxy"]
    if p["kernel"] == "fp-chromium":
        # 该内核的 UA/指纹由 --fingerprint 种子在内核层实时生成（Chrome 系），
        # 存储的 BrowserForge 指纹仅提供种子与目标 OS，不作为展示值
        out["fingerprint_summary"]["user_agent"] = None
        out["fingerprint_summary"]["kernel_driven"] = True
    return out


def _check_kernel_available(kernel: str) -> None:
    if kernel == "camoufox" and not camoufox_kernel.is_available()[0]:
        raise ApiError(400, "Camoufox 浏览器未安装，请先运行: python -m camoufox fetch")
    if kernel == "fp-chromium" and not fp_chromium_kernel.is_available()[0]:
        raise ApiError(400, fp_chromium_kernel.is_available()[1])


# ---------------------------------------------------------------- 系统 / 认证 / 成员

@app.get("/api/v1/status")
async def status() -> dict:
    cf_ok, cf_path = camoufox_kernel.is_available()
    ch_ok, ch_path = chromium_kernel.is_available()
    fp_ok, fp_path = fp_chromium_kernel.is_available()
    settings = security.load_settings()
    return ok({
        "app": APP_NAME,
        "version": VERSION,
        "phase": PHASE,
        "kernels": {
            "camoufox": {"available": cf_ok, "path": cf_path if cf_ok else None,
                         "error": None if cf_ok else cf_path},
            "chromium": {"available": ch_ok, "path": ch_path if ch_ok else None},
            "fp-chromium": {"available": fp_ok, "path": fp_path if fp_ok else None,
                            "error": None if fp_ok else fp_path},
        },
        "security": {"api_key_enabled": settings["api_key_enabled"],
                     "encryption": security.encryption_status()},
        "sync": {"server_enabled": settings.get("sync_server_enabled", False),
                 "auto_sync": settings.get("auto_sync", False)},
        "kernel_health": _kernel_health,
        "running_count": len(manager.active()),
    })


@app.post("/api/v1/auth/verify")
async def auth_verify(request: Request) -> dict:
    body = await request.json()
    key = body.get("api_key") or ""
    settings = security.load_settings()
    if settings.get("api_key_enabled"):
        member = db.get_member_by_key(key)
        return ok({"valid": bool(member),
                   "member": {"name": member["name"], "role": member["role"]} if member else None}) \
            if member else JSONResponse({"code": 40100, "msg": "API Key 无效", "data": None}, status_code=401)
    return ok({"valid": True, "member": {"name": "管理员", "role": ROLE_ADMIN}})


@app.post("/api/v1/pair/create")
async def pair_create(request: Request) -> dict:
    """生成插件配对码（5 分钟有效，一次性）。认证开启时返回当前成员的密钥配对。"""
    member = member_of(request)
    api_key = request.headers.get("x-api-key") or security.load_settings()["api_key"]
    code, ttl = pairing.create_pairing(api_key)
    audit("pair.create", "插件配对", f"有效期 {ttl}s", member=member)
    return ok({"pairing_code": code, "expires_in": ttl})


@app.post("/api/v1/pair/exchange")
async def pair_exchange(request: Request) -> dict:
    """配对码换取 API Key（无需认证；配对码本身即凭证，一次性 + 限尝试次数）。"""
    body = await request.json()
    api_key = pairing.exchange(body.get("pairing_code") or "")
    if not api_key:
        db.audit_log("pair.exchange", "插件配对", "配对码无效", "denied")
        return JSONResponse(
            {"code": 40101, "msg": "配对码无效或已过期（在工作台重新生成）", "data": None},
            status_code=401,
        )
    db.audit_log("pair.exchange", "插件配对", "配对成功")
    return ok({"api_key": api_key})


@app.get("/api/v1/me")
async def whoami(request: Request) -> dict:
    m = member_of(request)
    return ok({"id": m["id"], "name": m["name"], "role": m["role"],
               "auth_enabled": security.load_settings()["api_key_enabled"]})


@app.get("/api/v1/members")
async def list_members(request: Request) -> dict:
    require_admin(request)
    return ok(db.list_members())


@app.post("/api/v1/members")
async def create_member(request: Request, body: MemberCreate) -> dict:
    import secrets as _secrets

    member = require_admin(request)
    try:
        created = db.create_member(name=body.name, role=body.role,
                                   api_key=_secrets.token_hex(16))
    except Exception:
        raise ApiError(409, f"成员名已存在：{body.name}")
    audit("member.create", body.name, f"角色={body.role}", member=member)
    return ok({"id": created["id"], "name": created["name"], "role": created["role"],
               "api_key": created["api_key"]})  # 密钥仅创建时返回一次


@app.post("/api/v1/members/{member_id}/toggle")
async def toggle_member(request: Request, member_id: str) -> dict:
    admin = require_admin(request)
    target = next((m for m in db.list_members() if m["id"] == member_id), None)
    if not target:
        raise ApiError(404, "成员不存在")
    enabled = not target["enabled"]
    if not db.set_member_enabled(member_id, enabled):
        raise ApiError(409, "操作失败")
    audit("member.toggle", target["name"], f"启用={enabled}", member=admin)
    return ok({"id": member_id, "enabled": enabled})


@app.delete("/api/v1/members/{member_id}")
async def delete_member(request: Request, member_id: str) -> dict:
    admin = require_admin(request)
    if not db.delete_member(member_id):
        raise ApiError(409, "删除失败（最后一个启用的管理员不可删除）")
    audit("member.delete", member_id, member=admin)
    return ok()


@app.get("/api/v1/settings")
async def get_settings(request: Request) -> dict:
    settings = security.load_settings()
    return ok({
        "api_key_enabled": settings["api_key_enabled"],
        "api_key_masked": settings["api_key"][:4] + "****" + settings["api_key"][-4:],
        "sync_server_enabled": settings.get("sync_server_enabled", False),
        "sync_token_masked": (settings.get("sync_token") or "")[:4] + "****",
        "sync_remote_url": settings.get("sync_remote_url", ""),
        "sync_remote_configured": bool(settings.get("sync_remote_token")),
    })


@app.post("/api/v1/settings")
async def update_settings(request: Request, body: SettingsUpdate) -> dict:
    member = member_of(request)
    sync_cfg = {}
    if body.sync_server_enabled is not None:
        sync_cfg["sync_server_enabled"] = body.sync_server_enabled
    if body.sync_remote_url is not None:
        sync_cfg["sync_remote_url"] = body.sync_remote_url
    if body.sync_remote_token is not None:
        sync_cfg["sync_remote_token"] = body.sync_remote_token
    if body.regenerate_sync_token:
        sync_cfg["regenerate_sync_token"] = True

    if body.api_key_enabled is not None or body.regenerate_key:
        require_admin(request)

    settings = security.update_settings(
        api_key_enabled=body.api_key_enabled, regenerate=body.regenerate_key,
        sync=sync_cfg or None,
    )
    audit("settings.update", "系统设置",
          f"api_key_enabled={settings['api_key_enabled']} sync={sync_cfg or '无'}", member=member)
    out = {
        "api_key_enabled": settings["api_key_enabled"],
        "api_key": settings["api_key"] if body.regenerate_key else None,
        "sync_token": settings["sync_token"] if body.regenerate_sync_token else None,
        "sync_server_enabled": settings.get("sync_server_enabled"),
    }
    return ok(out)


@app.get("/api/v1/detect-links")
async def detect_links() -> dict:
    return ok(DETECT_LINKS)


@app.get("/api/v1/risk-presets")
async def risk_presets() -> dict:
    """风控环境预设说明（UI 展示用）。"""
    return ok(PRESET_DOC)


@app.post("/api/v1/profiles/{profile_id}/readiness")
async def profile_readiness(request: Request, profile_id: str) -> dict:
    """环境就绪度检测：实测 IP/时区/语言/WebRTC/webdriver/Canvas 等一致性（约 10~40 秒）。"""
    member = member_of(request)
    p = db.get_profile(profile_id)
    if not p:
        raise ApiError(404, "环境不存在")
    require_profile_access(request, p)
    try:
        report = await run_readiness(manager, p)
    except Exception as e:
        log.exception("就绪度检测失败")
        raise ApiError(500, f"检测失败: {e}")
    audit("profile.readiness", p["name"],
          f"得分 {report['score']}（{report['verdict_label']}）", member=member)
    return ok(report)


# ---------------------------------------------------------------- 环境 CRUD（含成员隔离）

@app.get("/api/v1/profiles")
async def list_profiles(request: Request, group: Optional[str] = None) -> dict:
    member = member_of(request)
    profiles = scoped_profiles(request)
    if group:
        profiles = [p for p in profiles if p["group_name"] == group]
    return ok([_serialize_profile(p) for p in profiles])


@app.post("/api/v1/profiles")
async def create_profile(request: Request, body: ProfileCreate) -> dict:
    member = member_of(request)
    _check_kernel_available(body.kernel)
    fingerprint = create_fingerprint(body.target_os, body.fingerprint_mode)
    p = db.create_profile(
        name=body.name,
        group_name=body.group_name,
        notes=body.notes,
        kernel=body.kernel,
        target_os=body.target_os,
        proxy=body.proxy.model_dump() if body.proxy else None,
        fingerprint=fingerprint,
        launch=body.launch.model_dump(),
        owner=member["id"],
    )
    audit("profile.create", body.name, f"内核={body.kernel} 模式={body.fingerprint_mode}", member=member)
    return ok(_serialize_profile(p, full=True))


@app.post("/api/v1/profiles/batch")
async def create_profiles_batch(request: Request, body: ProfileBatchCreate) -> dict:
    member = member_of(request)
    _check_kernel_available(body.template.kernel)
    created = []
    for i in range(body.count):
        proxy = None
        if body.proxy_pool:
            proxy = body.proxy_pool[min(i, len(body.proxy_pool) - 1)].model_dump()
        elif body.template.proxy:
            proxy = body.template.proxy.model_dump()
        fingerprint = create_fingerprint(
            body.template.target_os, body.template.fingerprint_mode
        )
        p = db.create_profile(
            name=f"{body.template.name}-{i + 1}",
            group_name=body.template.group_name,
            notes=body.template.notes,
            kernel=body.template.kernel,
            target_os=body.template.target_os,
            proxy=proxy,
            fingerprint=fingerprint,
            launch=body.template.launch.model_dump(),
            owner=member["id"],
        )
        created.append(_serialize_profile(p))
    audit("profile.batch_create", body.template.name, f"批量创建 {len(created)} 个环境", member=member)
    return ok({"count": len(created), "profiles": created})


@app.get("/api/v1/profiles/{profile_id}")
async def get_profile(request: Request, profile_id: str) -> dict:
    p = db.get_profile(profile_id)
    if not p:
        raise ApiError(404, "环境不存在")
    require_profile_access(request, p)
    return ok(_serialize_profile(p, full=True))


@app.put("/api/v1/profiles/{profile_id}")
async def update_profile(request: Request, profile_id: str, body: ProfileUpdate) -> dict:
    member = member_of(request)
    p = db.get_profile(profile_id)
    if not p:
        raise ApiError(404, "环境不存在")
    require_profile_access(request, p)
    if manager.is_running(profile_id):
        raise ApiError(409, "环境正在运行，请先停止再修改")

    updates: dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.group_name is not None:
        updates["group_name"] = body.group_name
    if body.notes is not None:
        updates["notes"] = body.notes
    if body.kernel is not None:
        updates["kernel"] = body.kernel
    if body.target_os is not None:
        updates["target_os"] = body.target_os
    if body.proxy is not None:
        updates["proxy_json"] = body.proxy.model_dump_json()
    if body.clear_proxy:
        updates["proxy_json"] = None
    if body.launch is not None:
        updates["launch_json"] = body.launch.model_dump_json()

    regen = body.regen_fingerprint
    if body.target_os and body.target_os != p["target_os"]:
        regen = True  # 换 OS 必须重生成指纹，否则穿帮
    if body.fingerprint_mode and body.fingerprint_mode != p["fingerprint"].get("mode"):
        regen = True
    if regen:
        import json as _json

        mode = body.fingerprint_mode or p["fingerprint"].get("mode", "generate")
        os_ = body.target_os or p["target_os"]
        updates["fingerprint_json"] = _json.dumps(
            create_fingerprint(os_, mode), ensure_ascii=False
        )

    updated = db.update_profile(profile_id, updates)
    if not updated:
        raise ApiError(404, "环境不存在")
    audit("profile.update", updated["name"], f"字段: {', '.join(updates) or '无'}", member=member)
    return ok(_serialize_profile(updated, full=True))


@app.delete("/api/v1/profiles/{profile_id}")
async def delete_profile(request: Request, profile_id: str) -> dict:
    member = member_of(request)
    if manager.is_running(profile_id):
        raise ApiError(409, "环境正在运行，请先停止再删除")
    p = db.get_profile(profile_id)
    if not p:
        raise ApiError(404, "环境不存在")
    require_profile_access(request, p)
    await pw_server.stop_server(profile_id)
    db.delete_profile(profile_id)
    import shutil

    user_data_dir = PROFILE_ROOT / profile_id
    if user_data_dir.exists():
        shutil.rmtree(user_data_dir, ignore_errors=True)
    audit("profile.delete", p["name"], member=member)
    return ok()


@app.post("/api/v1/profiles/batch-delete")
async def delete_profiles_batch(request: Request, body: IdsRequest) -> dict:
    member = member_of(request)
    import shutil

    results = []
    for pid in body.profile_ids:
        p = db.get_profile(pid)
        if not p:
            results.append({"profile_id": pid, "ok": False, "error": "不存在"})
            continue
        try:
            require_profile_access(request, p)
        except ApiError as e:
            results.append({"profile_id": pid, "ok": False, "error": e.msg})
            continue
        if manager.is_running(pid):
            results.append({"profile_id": pid, "ok": False, "error": "运行中，先停止"})
            continue
        await pw_server.stop_server(pid)
        db.delete_profile(pid)
        shutil.rmtree(PROFILE_ROOT / pid, ignore_errors=True)
        results.append({"profile_id": pid, "ok": True, "name": p["name"]})
    deleted = sum(1 for r in results if r["ok"])
    audit("profile.batch_delete", f"{deleted}/{len(results)}", "批量删除环境", member=member)
    return ok({"deleted": deleted, "results": results})


# ---------------------------------------------------------------- 指纹矩阵风控（管理员）

@app.get("/api/v1/matrix/report")
async def matrix_report(request: Request) -> dict:
    require_admin(request)
    return ok(matrix.build_report())


@app.post("/api/v1/matrix/regenerate")
async def matrix_regenerate(request: Request, body: IdsRequest) -> dict:
    member = require_admin(request)
    results = matrix.regenerate_risky(body.profile_ids)
    audit("matrix.regenerate", f"{sum(1 for r in results if r['ok'])} 个环境", "重生成风险指纹", member=member)
    return ok(results)


# ---------------------------------------------------------------- 导入 / 导出 / 备份

@app.get("/api/v1/profiles/{profile_id}/export")
async def export_profile(request: Request, profile_id: str, include_data: bool = False) -> dict:
    member = member_of(request)
    p = db.get_profile(profile_id)
    if not p:
        raise ApiError(404, "环境不存在")
    require_profile_access(request, p)
    data = transfer.export_profile(p, include_data=include_data)
    audit("profile.export", p["name"], f"包含数据目录={include_data}", member=member)
    return ok(data)


@app.post("/api/v1/profiles/import")
async def import_profile(request: Request, body: ProfileExportData) -> dict:
    member = member_of(request)
    try:
        imported = transfer.import_profile(body.model_dump(), owner=member["id"])
    except ValueError as e:
        raise ApiError(400, str(e))
    except Exception as e:
        raise ApiError(400, f"导入失败: {e}")
    audit("profile.import", imported["name"], "导入环境", member=member)
    return ok(_serialize_profile(imported, full=True))


@app.get("/api/v1/system/backup")
async def system_backup(request: Request) -> dict:
    member = require_admin(request)
    backup = transfer.backup_all()
    audit("system.backup", "整机备份", f"{backup['count']} 个环境", member=member)
    return ok(backup)


@app.post("/api/v1/system/restore")
async def system_restore(request: Request) -> dict:
    member = require_admin(request)
    body = await request.json()
    try:
        count = transfer.restore_all(body, wipe=True)
    except ValueError as e:
        raise ApiError(400, str(e))
    audit("system.restore", "整机恢复", f"恢复 {count} 个环境（已清空原数据）", member=member)
    return ok({"restored": count})


# ---------------------------------------------------------------- 同步

@app.post("/api/sync/upload")
async def sync_upload(request: Request) -> dict:
    body = await request.json()
    stats = sync.server_handle_upload(body)
    db.audit_log("sync.upload", body.get("node", "?"),
                 f"created={stats['created']} updated={stats['updated']} "
                 f"skipped={stats['skipped']} deleted={stats['deleted']}")
    return ok(stats)


@app.get("/api/sync/download")
async def sync_download(request: Request) -> dict:
    data = sync.server_handle_download()
    db.audit_log("sync.download", "节点拉取", f"{data['count']} 个环境")
    return ok(data)


@app.post("/api/v1/sync/push")
async def sync_push(request: Request) -> dict:
    member = require_admin(request)
    try:
        stats = await sync.push_to_remote()
    except ValueError as e:
        raise ApiError(400, str(e))
    except Exception as e:
        raise ApiError(502, f"推送失败: {e}")
    audit("sync.push", "同步推送", str(stats), member=member)
    return ok(stats)


@app.post("/api/v1/sync/pull")
async def sync_pull(request: Request) -> dict:
    member = require_admin(request)
    try:
        stats = await sync.pull_from_remote()
    except ValueError as e:
        raise ApiError(400, str(e))
    except Exception as e:
        raise ApiError(502, f"拉取失败: {e}")
    audit("sync.pull", "同步拉取", str(stats), member=member)
    return ok(stats)


# ---------------------------------------------------------------- 浏览器启动/停止

async def _start_one(request: Request, pid: str, headless: Optional[bool] = None,
                     start_url: Optional[str] = None) -> dict:
    p = db.get_profile(pid)
    if not p:
        return {"profile_id": pid, "ok": False, "error": "环境不存在"}
    try:
        require_profile_access(request, p)
    except ApiError as e:
        return {"profile_id": pid, "ok": False, "error": e.msg}
    try:
        info = await manager.start(p, headless=headless, start_url=start_url)
        return {"profile_id": pid, "ok": True, "name": p["name"], **info}
    except Exception as e:
        return {"profile_id": pid, "ok": False, "name": p["name"], "error": str(e)[:200]}


@app.post("/api/v1/browser/start")
async def browser_start(request: Request, body: BrowserStartRequest) -> dict:
    member = member_of(request)
    result = await _start_one(request, body.profile_id, body.headless, body.start_url)
    if not result["ok"]:
        if result["error"] == "环境不存在":
            raise ApiError(404, result["error"])
        if "已在运行" in result["error"]:
            raise ApiError(409, result["error"])
        raise ApiError(500, result["error"])
    audit("browser.start", result.get("name", body.profile_id), member=member)
    return ok(result)


@app.post("/api/v1/browser/start-batch")
async def browser_start_batch(request: Request, body: BatchStartRequest) -> dict:
    member = member_of(request)
    # 并行启动（launcher 内部信号量限流，避免批量串行久等）
    sem = asyncio.Semaphore(4)

    async def _one(pid: str) -> dict:
        async with sem:
            return await _start_one(request, pid, body.headless, body.start_url)

    results = await asyncio.gather(*[_one(pid) for pid in body.profile_ids])
    results = list(results)
    started = sum(1 for r in results if r["ok"])
    audit("browser.start_batch", f"{started}/{len(results)}", "批量启动环境", member=member)
    return ok({"started": started, "results": results})


@app.post("/api/v1/browser/stop")
async def browser_stop(request: Request, body: BrowserStartRequest) -> dict:
    member = member_of(request)
    if not manager.is_running(body.profile_id):
        raise ApiError(404, "该环境未在运行")
    p = db.get_profile(body.profile_id)
    if p:
        require_profile_access(request, p)
    await manager.stop(body.profile_id)
    audit("browser.stop", p["name"] if p else body.profile_id, member=member)
    return ok()


@app.post("/api/v1/browser/stop-batch")
async def browser_stop_batch(request: Request, body: IdsRequest) -> dict:
    member = member_of(request)
    results = []
    for pid in body.profile_ids:
        p = db.get_profile(pid)
        if not manager.is_running(pid):
            results.append({"profile_id": pid, "ok": False, "error": "未在运行"})
            continue
        try:
            require_profile_access(request, p)
        except ApiError as e:
            results.append({"profile_id": pid, "ok": False, "error": e.msg})
            continue
        await manager.stop(pid)
        results.append({"profile_id": pid, "ok": True})
    stopped = sum(1 for r in results if r["ok"])
    audit("browser.stop_batch", f"{stopped}/{len(results)}", "批量停止环境", member=member)
    return ok({"stopped": stopped, "results": results})


@app.get("/api/v1/browser/active")
async def browser_active(request: Request) -> dict:
    member = member_of(request)
    active = manager.active()
    if is_admin(member):
        return ok(active)
    mine = {p["id"] for p in db.list_profiles(owner=member["id"])}
    return ok([a for a in active if a["profile_id"] in mine])


# ---------------------------------------------------------------- Playwright 对接

@app.get("/api/v1/browser/{profile_id}/endpoint")
async def browser_endpoint(request: Request, profile_id: str) -> dict:
    """统一自动化端点：fp-chromium/chromium → CDP ws；camoufox → Playwright Server ws。"""
    p = db.get_profile(profile_id)
    if not p:
        raise ApiError(404, "环境不存在")
    require_profile_access(request, p)
    if p["kernel"] in ("fp-chromium", "chromium"):
        inst = manager.get_instance(profile_id)
        if not inst:
            raise ApiError(404, "环境未运行；启动后可获得 CDP 端点")
        return ok({
            "profile_id": profile_id,
            "protocol": "cdp",
            "ws_endpoint": inst.info.get("ws_endpoint"),
            "debug_port": inst.info.get("debug_port"),
            "connect_hint": "playwright.chromium.connect_over_cdp(ws_endpoint)",
        })
    info = pw_server.get_info(profile_id)
    if not info:
        raise ApiError(404, "Playwright Server 未运行；POST /browser/{id}/playwright-server 启动")
    return ok(info)


@app.post("/api/v1/browser/{profile_id}/playwright-server")
async def start_playwright_server(request: Request, profile_id: str,
                                  body: PlaywrightServerRequest | None = None) -> dict:
    member = member_of(request)
    p = db.get_profile(profile_id)
    if not p:
        raise ApiError(404, "环境不存在")
    require_profile_access(request, p)
    if p["kernel"] != "camoufox":
        raise ApiError(409, "该内核无需 Playwright Server：启动环境后用 /browser/{id}/endpoint "
                            "获取 CDP 端点，以 connect_over_cdp 直连")
    if not camoufox_kernel.is_available()[0]:
        raise ApiError(400, "Camoufox 浏览器未安装")
    try:
        info = await pw_server.start_server(p, port=body.port if body else None)
    except Exception as e:
        log.exception("Playwright Server 启动失败")
        raise ApiError(500, f"启动失败: {e}")
    audit("pw_server.start", p["name"], info["ws_endpoint"], member=member)
    return ok(info)


@app.delete("/api/v1/browser/{profile_id}/playwright-server")
async def stop_playwright_server(request: Request, profile_id: str) -> dict:
    member = member_of(request)
    if not await pw_server.stop_server(profile_id):
        raise ApiError(404, "该环境没有运行中的 Playwright Server")
    audit("pw_server.stop", profile_id, member=member)
    return ok()


@app.get("/api/v1/playwright-servers")
async def list_playwright_servers(request: Request) -> dict:
    require_admin(request)
    return ok(pw_server.list_servers())


# ---------------------------------------------------------------- 页面控制（所有内核通用）

@asynccontextmanager
async def _require_context(request: Request, profile_id: str):
    """获取可操作的页面：camoufox 直接用 inst.page；fp-chromium/chromium 通过 CDP 临时连接。

    用法：
        async with _require_context(request, pid) as page:
            await page.goto(...)
    对非 camoufox 内核，退出时自动断开临时 CDP 连接。
    """
    if not manager.is_running(profile_id):
        raise ApiError(404, "该环境未在运行")
    p = db.get_profile(profile_id)
    if p:
        require_profile_access(request, p)
    inst = manager.get_instance(profile_id)
    if not inst:
        raise ApiError(409, "浏览器实例不可用")

    # camoufox：直接持有 page 引用
    if inst.page is not None:
        yield inst.page
        return

    # fp-chromium / chromium：通过 CDP ws_endpoint 临时连接
    ws = inst.info.get("ws_endpoint")
    if not ws:
        raise ApiError(409, "该内核缺少 CDP 端点，无法进行页面控制")

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    try:
        conn_browser = await pw.chromium.connect_over_cdp(ws)
        context = conn_browser.contexts[0] if conn_browser.contexts else await conn_browser.new_context()
        pages = context.pages
        if not pages:
            page = await context.new_page()
        else:
            page = pages[0]
        yield page
    finally:
        try:
            await pw.stop()
        except Exception:
            pass


def _require_page(request: Request, profile_id: str):
    """向后兼容：仅 camoufox 内核的简单场景（navigate/screenshot/evaluate）。"""
    if not manager.is_running(profile_id):
        raise ApiError(404, "该环境未在运行")
    p = db.get_profile(profile_id)
    if p:
        require_profile_access(request, p)
    inst = manager.get_instance(profile_id)
    if not inst or inst.page is None:
        raise ApiError(409, "该内核不支持直接页面控制（仅 camoufox）；请使用新增的通用页面交互接口")
    return inst


@app.post("/api/v1/browser/{profile_id}/navigate")
async def browser_navigate(request: Request, profile_id: str, body: NavigateRequest) -> dict:
    async with _require_context(request, profile_id) as page:
        try:
            await page.goto(body.url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            raise ApiError(500, f"导航失败: {e}")
        return ok({"url": page.url})


@app.post("/api/v1/browser/{profile_id}/screenshot")
async def browser_screenshot(request: Request, profile_id: str,
                             body: ScreenshotRequest | None = None) -> dict:
    import base64

    async with _require_context(request, profile_id) as page:
        shot = await page.screenshot(full_page=bool(body.full_page if body else False))
    return ok({"format": "png", "base64": base64.b64encode(shot).decode()})


@app.post("/api/v1/browser/{profile_id}/evaluate")
async def browser_evaluate(request: Request, profile_id: str, body: EvaluateRequest) -> dict:
    async with _require_context(request, profile_id) as page:
        try:
            value = await page.evaluate(body.expression)
        except Exception as e:
            raise ApiError(500, f"执行失败: {e}")
    return ok({"result": value})


@app.post("/api/v1/browser/{profile_id}/click")
async def browser_click(request: Request, profile_id: str, body: ClickRequest) -> dict:
    async with _require_context(request, profile_id) as page:
        await page.locator(body.selector).first.click(timeout=body.timeout)
    return ok({"detail": f"已点击 {body.selector}"})


@app.post("/api/v1/browser/{profile_id}/type")
async def browser_type(request: Request, profile_id: str, body: TypeRequest) -> dict:
    async with _require_context(request, profile_id) as page:
        loc = page.locator(body.selector).first
        await loc.fill("", timeout=body.timeout)
        await loc.fill(body.text, timeout=body.timeout)
        if body.press_enter:
            await loc.press("Enter")
    return ok({"detail": f"已输入 {len(body.text)} 字符"})


@app.post("/api/v1/browser/{profile_id}/press")
async def browser_press(request: Request, profile_id: str, body: PressRequest) -> dict:
    async with _require_context(request, profile_id) as page:
        await page.keyboard.press(body.key)
    return ok({"detail": f"已按键 {body.key}"})


@app.post("/api/v1/browser/{profile_id}/wait")
async def browser_wait(request: Request, profile_id: str, body: WaitRequest) -> dict:
    import asyncio
    await asyncio.sleep(body.ms / 1000)
    return ok({"detail": f"等待 {body.ms}ms"})


@app.post("/api/v1/browser/{profile_id}/wait_for")
async def browser_wait_for(request: Request, profile_id: str, body: WaitForRequest) -> dict:
    async with _require_context(request, profile_id) as page:
        await page.locator(body.selector).first.wait_for(timeout=body.timeout)
    return ok({"detail": f"元素已出现 {body.selector}"})


@app.post("/api/v1/browser/{profile_id}/scroll")
async def browser_scroll(request: Request, profile_id: str, body: ScrollRequest) -> dict:
    async with _require_context(request, profile_id) as page:
        await page.mouse.wheel(0, body.amount)
    return ok({"detail": f"滚动 {body.amount}px"})


@app.post("/api/v1/browser/{profile_id}/hover")
async def browser_hover(request: Request, profile_id: str, body: HoverRequest) -> dict:
    async with _require_context(request, profile_id) as page:
        await page.locator(body.selector).first.hover(timeout=body.timeout)
    return ok({"detail": f"已悬停 {body.selector}"})


@app.post("/api/v1/browser/{profile_id}/select")
async def browser_select(request: Request, profile_id: str, body: SelectRequest) -> dict:
    async with _require_context(request, profile_id) as page:
        loc = page.locator(body.selector).first
        try:
            await loc.select_option(body.value, timeout=body.timeout)
        except Exception:
            await loc.select_option(label=body.value, timeout=body.timeout)
    return ok({"detail": f"已选择 {body.value}"})


@app.post("/api/v1/browser/{profile_id}/extract")
async def browser_extract(request: Request, profile_id: str, body: ExtractRequest) -> dict:
    async with _require_context(request, profile_id) as page:
        if body.attr:
            values = await page.eval_on_selector_all(
                body.selector, f"els => els.map(e => e.getAttribute({body.attr!r}))"
            )
        else:
            values = await page.eval_on_selector_all(
                body.selector, "els => els.map(e => (e.textContent || '').trim())",
            )
    return ok({"values": values[:50], "count": len(values)})


@app.post("/api/v1/browser/{profile_id}/page_info")
async def browser_page_info(request: Request, profile_id: str) -> dict:
    async with _require_context(request, profile_id) as page:
        title = await page.title()
        url = page.url
        cookies = await page.context.cookies()
    return ok({"title": title, "url": url, "cookie_count": len(cookies),
               "cookies": [{"name": c["name"], "value": c["value"], "domain": c["domain"],
                             "path": c["path"]} for c in cookies[:100]]})


@app.post("/api/v1/browser/{profile_id}/get_html")
async def browser_get_html(request: Request, profile_id: str, body: GetHtmlRequest) -> dict:
    async with _require_context(request, profile_id) as page:
        html = await page.content()
    truncated = len(html) > body.max_length
    return ok({"html": html[:body.max_length], "total_length": len(html), "truncated": truncated})


# ---------------------------------------------------------------- RPA 任务（owner 隔离）

def _require_task_access(request: Request, task: dict) -> dict:
    member = member_of(request)
    if not is_admin(member) and task.get("owner", "admin") != member["id"]:
        raise ApiError(403, "无权访问该任务（仅创建者或管理员）")
    return task


@app.get("/api/v1/tasks")
async def list_tasks(request: Request) -> dict:
    member = member_of(request)
    tasks = db.list_tasks(owner=None if is_admin(member) else member["id"])
    return ok([{**t, "steps_count": len(t["steps"])} for t in tasks])


@app.post("/api/v1/tasks")
async def create_task(request: Request, body: TaskCreate) -> dict:
    member = member_of(request)
    steps = [s.model_dump(exclude_none=True) for s in body.steps]
    errors = validate_steps(steps)
    if errors:
        raise ApiError(400, "；".join(errors))
    t = db.create_task(name=body.name, notes=body.notes, steps=steps,
                       webhook_url=body.webhook_url, owner=member["id"])
    audit("task.create", body.name, f"{len(steps)} 个步骤", member=member)
    return ok(t)


@app.get("/api/v1/tasks/{task_id}")
async def get_task(request: Request, task_id: str) -> dict:
    t = db.get_task(task_id)
    if not t:
        raise ApiError(404, "任务不存在")
    _require_task_access(request, t)
    return ok(t)


@app.put("/api/v1/tasks/{task_id}")
async def update_task(request: Request, task_id: str, body: TaskUpdate) -> dict:
    member = member_of(request)
    t = db.get_task(task_id)
    if not t:
        raise ApiError(404, "任务不存在")
    _require_task_access(request, t)
    updates: dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.notes is not None:
        updates["notes"] = body.notes
    if body.webhook_url is not None:
        updates["webhook_url"] = body.webhook_url
    if body.steps is not None:
        steps = [s.model_dump(exclude_none=True) for s in body.steps]
        errors = validate_steps(steps)
        if errors:
            raise ApiError(400, "；".join(errors))
        updates["steps"] = steps
    t = db.update_task(task_id, updates)
    if not t:
        raise ApiError(404, "任务不存在")
    audit("task.update", t["name"], f"字段: {', '.join(updates) or '无'}", member=member)
    return ok(t)


@app.delete("/api/v1/tasks/{task_id}")
async def delete_task(request: Request, task_id: str) -> dict:
    member = member_of(request)
    t = db.get_task(task_id)
    if not t:
        raise ApiError(404, "任务不存在")
    _require_task_access(request, t)
    if not db.delete_task(task_id):
        raise ApiError(404, "任务不存在")
    audit("task.delete", t["name"], member=member)
    return ok()


@app.post("/api/v1/tasks/{task_id}/run")
async def run_task(request: Request, task_id: str, body: TaskRunRequest) -> dict:
    member = member_of(request)
    t = db.get_task(task_id)
    if not t:
        raise ApiError(404, "任务不存在")
    profiles = []
    for pid in body.profile_ids:
        p = db.get_profile(pid)
        if not p:
            raise ApiError(404, f"环境不存在: {pid}")
        require_profile_access(request, p)
        profiles.append(p)
    run_ids = await task_engine.run(t, profiles, headless=body.headless,
                                    auto_close=body.auto_close,
                                    humanize=body.humanize)
    audit("task.run", t["name"],
          f"{len(profiles)} 个环境, runs={run_ids}" + ("（人机化节奏）" if body.humanize else ""),
          member=member)
    return ok({"run_ids": run_ids, "count": len(run_ids)})


@app.get("/api/v1/task-runs")
async def list_task_runs(request: Request, task_id: Optional[str] = None,
                         profile_id: Optional[str] = None,
                         limit: int = 50) -> dict:
    member = member_of(request)
    runs = db.list_runs(task_id=task_id, profile_id=profile_id, limit=limit)
    if not is_admin(member):
        # operator 只能看到自己任务/环境的运行记录
        my_tasks = {t["id"] for t in db.list_tasks(owner=member["id"])}
        my_profiles = {p["id"] for p in db.list_profiles(owner=member["id"])}
        runs = [r for r in runs if r["task_id"] in my_tasks or r["profile_id"] in my_profiles]
    return ok(runs)


@app.get("/api/v1/task-runs/{run_id}")
async def get_task_run(request: Request, run_id: str) -> dict:
    member = member_of(request)
    r = db.get_run(run_id)
    if not r:
        raise ApiError(404, "运行记录不存在")
    if not is_admin(member):
        t = db.get_task(r["task_id"])
        p = db.get_profile(r["profile_id"])
        owned = (t or {}).get("owner") == member["id"] or (p or {}).get("owner") == member["id"]
        if not owned:
            raise ApiError(403, "无权访问该运行记录")
    return ok(r)


@app.post("/api/v1/task-runs/{run_id}/cancel")
async def cancel_task_run(run_id: str) -> dict:
    if not db.get_run(run_id):
        raise ApiError(404, "运行记录不存在")
    if not task_engine.cancel(run_id):
        raise ApiError(409, "该运行已结束，无法取消")
    return ok()


# ---------------------------------------------------------------- 任务模板

TASK_TEMPLATES = {
    "warmup": {
        "name": "环境预热（养环境）",
        "notes": "访问高流量中性站点积累浏览历史与信誉，建议新建环境后以有头模式运行数次",
        "steps": [
            {"action": "navigate", "url": "https://www.baidu.com", "timeout": 60000},
            {"action": "wait", "ms": 4000},
            {"action": "scroll", "amount": 600},
            {"action": "wait", "ms": 3000},
            {"action": "navigate", "url": "https://www.bing.com", "timeout": 60000},
            {"action": "wait", "ms": 4000},
            {"action": "scroll", "amount": -300},
            {"action": "navigate", "url": "https://example.com", "timeout": 60000},
            {"action": "wait", "ms": 2000},
        ],
    },
}


@app.get("/api/v1/task-templates")
async def list_task_templates() -> dict:
    return ok({k: {"name": v["name"], "notes": v["notes"],
                   "steps_count": len(v["steps"])} for k, v in TASK_TEMPLATES.items()})


@app.post("/api/v1/task-templates/{template_id}/create")
async def create_task_from_template(request: Request, template_id: str) -> dict:
    member = member_of(request)
    tpl = TASK_TEMPLATES.get(template_id)
    if not tpl:
        raise ApiError(404, "模板不存在")
    t = db.create_task(name=tpl["name"], notes=tpl["notes"], steps=tpl["steps"],
                       owner=member["id"])
    audit("task.create", tpl["name"], f"模板 {template_id}，{len(tpl['steps'])} 个步骤", member=member)
    return ok(t)


# ---------------------------------------------------------------- 定时调度（管理员）

@app.get("/api/v1/schedules")
async def list_schedules(request: Request) -> dict:
    require_admin(request)
    from .scheduler import Scheduler as S

    return ok([{**s, "next_run_at": S.next_run_at(s), "describe": S.describe(s)}
               for s in db.list_schedules()])


@app.post("/api/v1/schedules")
async def create_schedule(request: Request, body: ScheduleCreate) -> dict:
    member = require_admin(request)
    if not db.get_task(body.task_id):
        raise ApiError(404, "任务不存在")
    if body.kind == "daily" and not body.daily_time:
        raise ApiError(400, "每日调度必须提供 daily_time（HH:MM）")
    if body.kind == "interval" and not body.interval_minutes:
        raise ApiError(400, "间隔调度必须提供 interval_minutes")
    if body.timezone:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(body.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ApiError(400, f"无效时区: {body.timezone}（需 IANA 名，如 Asia/Shanghai）")
    for pid in body.profile_ids:
        if not db.get_profile(pid):
            raise ApiError(404, f"环境不存在: {pid}")
    s = db.create_schedule(
        name=body.name, task_id=body.task_id, kind=body.kind,
        interval_minutes=body.interval_minutes, daily_time=body.daily_time,
        profile_ids=body.profile_ids, headless=body.headless, auto_close=body.auto_close,
        timezone=body.timezone, weekdays=body.weekdays,
    )
    audit("schedule.create", body.name, f"kind={body.kind}", member=member)
    return ok(s)


@app.put("/api/v1/schedules/{schedule_id}")
async def update_schedule(request: Request, schedule_id: str, body: ScheduleUpdate) -> dict:
    require_admin(request)
    updates = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not updates:
        return ok(db.get_schedule(schedule_id))
    s = db.update_schedule(schedule_id, updates)
    if not s:
        raise ApiError(404, "调度不存在")
    return ok(s)


@app.delete("/api/v1/schedules/{schedule_id}")
async def delete_schedule(request: Request, schedule_id: str) -> dict:
    member = require_admin(request)
    if not db.delete_schedule(schedule_id):
        raise ApiError(404, "调度不存在")
    audit("schedule.delete", schedule_id, member=member)
    return ok()


@app.post("/api/v1/schedules/{schedule_id}/run-now")
async def run_schedule_now(request: Request, schedule_id: str) -> dict:
    member = require_admin(request)
    count = await scheduler.run_now(schedule_id, reason="manual")
    if count == 0:
        raise ApiError(409, "没有可运行的环境（任务或环境可能已被删除）")
    audit("schedule.run_now", schedule_id, f"{count} 个运行", member=member)
    return ok({"submitted": count})


# ---------------------------------------------------------------- 审计日志 / 代理

@app.get("/api/v1/audit-logs")
async def audit_logs(request: Request, action: Optional[str] = None, limit: int = 100,
                     offset: int = 0) -> dict:
    require_admin(request)
    return ok(db.list_audit_logs(action=action, limit=min(limit, 500), offset=offset))


@app.post("/api/v1/proxy/test")
async def proxy_test(body: ProxyTestRequest) -> dict:
    return ok(await test_proxy(body.proxy, body.timeout))


# ---------------------------------------------------------------- 静态资源（最后挂载，避免吞掉 API 路由）

(DATA_DIR / "runs").mkdir(parents=True, exist_ok=True)
app.mount("/runs", StaticFiles(directory=DATA_DIR / "runs"), name="runs")
app.mount("/intro", StaticFiles(directory=BASE_DIR / "site", html=True), name="intro")
app.mount("/", StaticFiles(directory=BASE_DIR / "app" / "static", html=True), name="static")
