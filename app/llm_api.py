"""API transports and local operator settings. Credentials stay in server memory."""

import copy
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
import urllib.error
import urllib.request
from pathlib import Path
from .generation import generation_config
from .store import bindings_for, can_action, connect, user_record

API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4.1-mini-2025-04-14"
PROVIDERS = {
    "openai": {"name": "OpenAI", "url": API_URL, "default_model": DEFAULT_MODEL},
    "groq": {
        "name": "Groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "default_model": "openai/gpt-oss-120b",
    },
}
_lock = threading.RLock()
_credentials = {}


class ApiFailure(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request, timeout):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def _db_key(db):
    return str(Path(db).resolve())


def _authorize(db, user_id):
    with connect(db) as conn:
        user = user_record(conn, user_id)
        if not user or not can_action(user, "system.configure_llm", bindings_for(conn)):
            raise PermissionError("Bạn chưa được cấp quyền cấu hình LLM.")


def ready(db, config=None):
    config = config or generation_config(db) or {}
    if config.get("provider") not in PROVIDERS:
        return True
    with _lock:
        entry = _credentials.get(_db_key(db))
        return bool(
            entry
            and entry["revision"] == config.get("credential_revision")
            and entry["provider"] == config["provider"]
        )


def status(db, user_id):
    _authorize(db, user_id)
    config = generation_config(db) or {}
    return {
        "provider": config.get("provider", "ollama"),
        "model": config.get("model"),
        "key_ready": ready(db, config),
        "api_url": PROVIDERS.get(config.get("provider"), PROVIDERS["openai"])["url"],
        "default_model": DEFAULT_MODEL,
        "credential_storage": "server_memory",
        "embedding": "local",
        "providers": [{"id": key, **value} for key, value in PROVIDERS.items()],
    }


def _schema(schema):
    # The strict API supports a subset of JSON Schema. Length/uniqueness checks
    # remain enforced by the backend after decoding, independent of the model.
    value = copy.deepcopy(schema)

    def clean(node):
        if isinstance(node, dict):
            for key in ("maxLength", "minLength", "uniqueItems", "maxItems", "minItems"):
                node.pop(key, None)
            for child in node.values():
                clean(child)
        elif isinstance(node, list):
            for child in node:
                clean(child)

    clean(value)
    return value


def _completion(key, model, messages, schema, limit, timeout=45, provider="openai"):
    if provider not in PROVIDERS:
        raise ApiFailure("Nhà cung cấp API chưa được hỗ trợ.")
    provider_info = PROVIDERS[provider]
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_completion_tokens": limit,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "grounded_answer", "strict": True, "schema": _schema(schema)},
        },
    }
    if provider == "openai":
        payload["store"] = False
    elif provider == "groq":
        payload.update(temperature=0.0, seed=42)
        if model.startswith("openai/gpt-oss-"):
            payload.update(reasoning_effort="low", include_reasoning=False)
    request = urllib.request.Request(
        provider_info["url"],
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "mission2-mvp/1.0",
            "Authorization": "Bearer " + key,
        },
    )
    start = time.perf_counter()
    try:
        with _open(request, timeout) as response:
            data = response.read(1000001)
            if len(data) > 1000000:
                raise ApiFailure("API trả dữ liệu vượt giới hạn; chưa phát kết luận.")
            raw = json.loads(data)
        choice = raw["choices"][0]
        message = choice["message"]
        if message.get("refusal"):
            raise ApiFailure("API từ chối xử lý nội dung này; chưa phát kết luận.")
        if choice.get("finish_reason") != "stop" or not isinstance(message.get("content"), str):
            raise ApiFailure("API chưa hoàn tất đáp án; chưa phát kết luận.")
        usage = raw.get("usage", {})
        return {
            "message": {"content": message["content"]},
            "prompt_eval_count": usage.get("prompt_tokens"),
            "eval_count": usage.get("completion_tokens"),
            "total_duration": (time.perf_counter() - start) * 1e9,
            "done_reason": "stop",
            "model": raw.get("model", model),
            "provider": provider,
        }
    except urllib.error.HTTPError as exc:
        messages = {
            401: "API key chưa hợp lệ.",
            403: "API từ chối truy cập (403); kiểm tra quyền key/model hoặc kết nối.",
            404: "Model API không khả dụng.",
            429: "API hết quota hoặc vượt giới hạn; kiểm tra billing/usage.",
        }
        # Never echo provider bodies, request headers, credentials or exceptions.
        raise ApiFailure(
            messages.get(exc.code, provider_info["name"] + " API tạm lỗi; vui lòng thử lại.")
        ) from None
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        raise ApiFailure(
            "Chưa kết nối được "
            + provider_info["name"]
            + " API hoặc phản hồi không hợp lệ; vui lòng thử lại."
        ) from None


def complete(db, config, messages, schema, limit):
    provider = config.get("provider")
    if provider not in PROVIDERS or config.get("url") != PROVIDERS[provider]["url"]:
        raise ApiFailure("Địa chỉ API không hợp lệ; chưa gửi nguồn.")
    with _lock:
        entry = _credentials.get(_db_key(db))
        if (
            not entry
            or entry["revision"] != config.get("credential_revision")
            or entry["provider"] != provider
        ):
            raise ApiFailure("Chưa có API key trong phiên server. Mở Cấu hình LLM để nhập lại.")
        key = entry["key"]
    return _completion(
        key,
        config["model"],
        messages,
        schema,
        limit,
        config.get("timeout_seconds", 45),
        provider=provider,
    )


def configure(db, payload):
    uid = payload.get("user_id")
    _authorize(db, uid)
    provider = payload.get("provider")
    if provider not in (*PROVIDERS, "ollama"):
        raise ValueError("Chọn OpenAI, Groq hoặc local.")
    if provider in PROVIDERS:
        provider_info = PROVIDERS[provider]
        if payload.get("consent") is not True:
            raise ValueError(
                "Cần xác nhận gửi câu hỏi và các đoạn nguồn được phép tới "
                + provider_info["name"]
                + " để dùng API."
            )
        key = payload.get("api_key", "")
        model = payload.get("model", provider_info["default_model"])
        if (
            not isinstance(key, str)
            or not 12 <= len(key.strip()) <= 512
            or any(c.isspace() for c in key.strip())
        ):
            raise ValueError("Nhập API key hợp lệ trực tiếp trong ô bảo mật.")
        key = key.strip()
        if not key.startswith("gsk_" if provider == "groq" else "sk-"):
            raise ValueError(
                "Key không đúng định dạng của " + provider_info["name"] + "; chưa gửi key ra ngoài."
            )
        if not isinstance(model, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,100}", model
        ):
            raise ValueError("Tên model không hợp lệ.")
        # Test with synthetic data only. Do not send any document during activation.
        try:
            probe = _completion(
                key,
                model,
                [{"role": "user", "content": "Return JSON with ok=true."}],
                {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
                512 if provider == "groq" else 30,
                provider=provider,
            )
            if json.loads(probe["message"]["content"]) != {"ok": True}:
                raise ValueError()
        except ApiFailure as exc:
            raise ValueError(str(exc)) from None
        except (ValueError, TypeError):
            raise ValueError("API chưa xác nhận được kết nối; cấu hình cũ được giữ.") from None
        _authorize(db, uid)  # Permission may have changed during the network call.
        with _lock:
            revision = uuid.uuid4().hex
            with connect(db) as conn:
                current = generation_config(db)
                if current and current.get("provider", "ollama") == "ollama":
                    conn.execute(
                        "INSERT OR REPLACE INTO settings VALUES(?,?)",
                        ("local_generation_config", json.dumps(current)),
                    )
                config = {
                    "enabled": True,
                    "provider": provider,
                    "url": provider_info["url"],
                    "model": model,
                    "digest": provider + ":" + model,
                    "credential_revision": revision,
                    "num_ctx": 32000,
                    "timeout_seconds": 45,
                }
                if provider == "groq":
                    config.update(
                        synthesis_tokens=2400, verification_tokens=1024, reasoning_effort="low"
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO settings VALUES(?,?)",
                    ("generation_config", json.dumps(config)),
                )
            _credentials[_db_key(db)] = {"revision": revision, "provider": provider, "key": key}
    else:
        with _lock:
            with connect(db) as conn:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key='local_generation_config'"
                ).fetchone()
                current = generation_config(db)
                if row:
                    config = json.loads(row[0])
                elif current and current.get("provider", "ollama") == "ollama":
                    config = current
                else:
                    raise ValueError("Chưa có cấu hình model local để khôi phục.")
                conn.execute(
                    "INSERT OR REPLACE INTO settings VALUES(?,?)",
                    ("generation_config", json.dumps(config)),
                )
            _credentials.pop(_db_key(db), None)
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO audit(actor_id,document_id,action,occurred_at,detail_json) VALUES(?,?,?,?,?)",
            (
                uid,
                "SYSTEM",
                "system.configure_llm",
                datetime.now(timezone.utc).isoformat(),
                json.dumps(
                    {
                        "provider": provider,
                        "model": config["model"],
                        "credential_storage": "server_memory",
                    }
                ),
            ),
        )
    return status(db, uid)
