import hashlib
import hmac
import os

import httpx

from avatar_contract import VERSION, base_url

from .db import ServiceError, identifier, now, set_setting, setting


def text(value):
    return value.strip() if isinstance(value, str) else ""


def avatar_url():
    return base_url(os.getenv("HR_AVATAR_BASE_URL", "http://127.0.0.1:8788"))


def public_url():
    return base_url(os.getenv("HR_PUBLIC_URL", "http://127.0.0.1:8891"))


def setup_code(store, reopen=False):
    with store.connection(write=True) as db:
        if not reopen:
            existing = setting(db, "setup.code")
            if existing:
                return store.open(existing)
            if setting(db, "service.key"):
                raise ServiceError("已配对。如需重新配对，请使用 reopen-setup", 409)
        code = identifier("setup")
        set_setting(db, "setup.code", store.seal(code))
        return code


def post_avatar(path, payload, origin=None):
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            reply = client.post((origin or avatar_url()) + path, json=payload)
    except httpx.RequestError:
        raise ServiceError("无法连接 Avatar 身份服务", 502) from None
    if reply.status_code in (401, 403):
        raise ServiceError("Avatar 凭据无效或已失效", 401)
    if not reply.is_success:
        raise ServiceError("Avatar 身份服务调用失败", 502)
    try:
        value = reply.json()
    except ValueError:
        raise ServiceError("Avatar 身份服务返回了无效 JSON", 502) from None
    if not isinstance(value, dict):
        raise ServiceError("Avatar 身份服务返回了无效 JSON", 502)
    return value


def matches(a, b):
    return hmac.compare_digest(hashlib.sha256(a.encode()).digest(), hashlib.sha256(b.encode()).digest())


def accept_setup(store, body, origin=None, invoke_base=None):
    service = body.get("service")
    credentials = body.get("credentials")
    avatar = body.get("avatar")
    if not all(isinstance(value, dict) for value in (service, credentials, avatar)):
        raise ServiceError("setup 缺少 service、avatar 或 credentials")
    service = {field: text(service.get(field)) for field in ("id", "slug", "name")}
    key = text(credentials.get("serviceKey"))
    if not key or not all(service.values()):
        raise ServiceError("setup 缺少服务信息或永久密钥")
    expected_origin = origin or avatar_url()
    for field, target in (("baseUrl", expected_origin), ("identityCheckUrl", expected_origin + "/identitycheck"),
                          ("serviceCheckUrl", expected_origin + "/servicecheck")):
        if avatar.get(field) != target:
            raise ServiceError("Avatar 回调地址与 HR_AVATAR_BASE_URL 不一致")
    response = {
        "ok": True, "protocolVersion": VERSION, "service": service,
        "invoke": {"transport": "http", "target": (invoke_base or public_url()) + "/avatar"},
    }
    with store.connection() as db:
        encrypted_code = setting(db, "setup.code")
        encrypted_key = setting(db, "service.key")
        same_pairing = (
            setting(db, "service.id") == service["id"] and encrypted_key
            and matches(store.open(encrypted_key), key)
        )
    if encrypted_code is None:
        if same_pairing:
            return response
        raise ServiceError("setup 已关闭，请在 HR 服务本机打开配对", 409)
    code = text(body.get("setupCode"))
    if not code or not matches(store.open(encrypted_code), code):
        raise ServiceError("一次性 setup code 无效", 401)
    options = {"origin": origin} if origin else {}
    verified = post_avatar("/servicecheck", {"serviceKey": key}, **options)
    confirmed = verified.get("service")
    if verified.get("ok") is not True or not isinstance(confirmed, dict) or confirmed.get("id") != service["id"]:
        raise ServiceError("Avatar 未确认这份服务密钥", 401)
    with store.connection(write=True) as db:
        if setting(db, "setup.code") != encrypted_code:
            raise ServiceError("setup code 已变更或已被使用", 409)
        for field, value in service.items():
            set_setting(db, "service." + field, value)
        set_setting(db, "service.key", store.seal(key))
        set_setting(db, "paired.at", str(now()))
        db.execute("DELETE FROM settings WHERE key = 'setup.code'")
    return response


def authenticate(store, user_key, origin=None, *, action=None, request_id=None,
                 operation=None, purpose=None, require_grant=False):
    with store.connection() as db:
        key = setting(db, "service.key")
    if not key:
        raise ServiceError("HR 服务尚未与 Avatar 配对", 503)
    if not user_key:
        raise ServiceError("缺少 Avatar 用户服务密钥", 401)
    options = {"origin": origin} if origin else {}
    reply = post_avatar("/identitycheck", {"serviceKey": store.open(key), "userKey": user_key}, **options)
    user = reply.get("user")
    if (reply.get("ok") is not True or not isinstance(user, dict)
            or not text(user.get("id")) or not text(user.get("handle"))):
        raise ServiceError("Avatar 身份校验失败", 401)
    grant = {"action": action, "requestId": request_id}
    if operation is not None:
        grant.update(operationId=operation, purpose=purpose)
    if (require_grant or "grant" in reply) and reply.get("grant") != grant:
        raise ServiceError("该用户服务密钥未授权本次请求", 403)
    return {
        "id": user["id"], "handle": user["handle"],
        "displayName": text(user.get("displayName")) or user["handle"],
        "role": "admin" if user.get("role") == "admin" else "member",
        "jobTitle": text(user.get("jobTitle")),
        "contactable": user.get("contactable") is not False,
        **{key: user[key] for key in ("rank", "bio") if isinstance(user.get(key), str)},
    }

