from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
from pathlib import Path


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    # Windows 读者可能暂时锁住目标；只重试本地替换，不重发模型请求。
    for attempt in range(7):
        try:
            temporary.replace(path)
            return
        except PermissionError as exc:
            if attempt == 6:
                exc.add_note("Atomic replace exhausted local retries; complete temporary file retained: " + str(temporary))
                raise
            time.sleep(min(0.05 * (2 ** attempt), 1.0))


class BudgetPaused(RuntimeError):
    pass


class BatchFailed(RuntimeError):
    def __init__(self, message, kind="request_error"):
        super().__init__(message)
        self.kind = kind


class OpenAIJSONClient:
    """单次请求；上层负责限额、缓存和结构修复。"""
    def __init__(self, config):
        self.options = dict(config.get("llm", config))

    def call_json(self, stage, payload, instruction):
        opts = self.options
        key = os.environ.get(opts.get("api_key_env", "SILICONFLOW_API_KEY"))
        if not key:
            raise RuntimeError("Required API credential environment variable is absent")
        body = dict(opts.get("extra_body") or {})
        body.update(model=opts["model"], temperature=opts.get("temperature", 0),
                    max_tokens=opts.get("max_tokens", 8000),
                    messages=[{"role": "system", "content": instruction},
                              {"role": "user", "content": dumps(payload)}],
                    response_format={"type": "json_object"})
        if "top_p" in opts:
            body["top_p"] = opts["top_p"]
        request = urllib.request.Request(opts["base_url"].rstrip("/") + "/chat/completions",
                                         data=dumps(body).encode("utf-8"),
                                         headers={"Authorization": "Bearer " + key,
                                                  "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=opts.get("timeout_seconds", 600)) as response:
            raw = json.loads(response.read().decode("utf-8"))
        content = raw["choices"][0]["message"]["content"]
        try:
            result = json.loads(content)
        except (ValueError, TypeError):
            result = {"_parse_error": True}
        if not isinstance(result, dict):
            result = {"_parse_error": True}
        result["_usage"] = raw.get("usage", {})
        result["_raw_response"] = raw
        result["_finish_reason"] = raw["choices"][0].get("finish_reason")
        return result


class RequestStore:
    def __init__(self, output, config, fingerprint, client):
        self.root = Path(output)
        self.config = config
        self.client = client
        self.fingerprint = fingerprint
        self.lock = threading.Lock()
        self.path = self.root / "usage.json"
        self.usage = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {"requests": 0, "charged_tokens": 0, "events": []}

    def _request(self, stage, payload, instruction, key):
        response_path = self.root / "request_cache" / (key + ".json")
        with self.lock:
            if response_path.exists():
                cached = json.loads(response_path.read_text("utf-8"))
                if cached["status"] == "response":
                    return cached["response"]
                raise BatchFailed("Request previously failed or interrupted; explicit new run required: " + key)
            # 按 UTF-8 字节上界预留输入成本，未知用量不得当成零。
            reserve = len((instruction + dumps(payload)).encode("utf-8")) + 1024 + int(self.config.get("llm", {}).get("max_tokens", 8000))
            if self.usage["requests"] >= int(self.config.get("max_requests", 1000)):
                raise BudgetPaused("Cumulative request limit reached")
            if self.usage["charged_tokens"] + reserve > int(self.config.get("max_total_tokens", 10000000)):
                raise BudgetPaused("Cumulative token guard reached")
            self.usage["requests"] += 1
            self.usage["charged_tokens"] += reserve
            event = {"key": key, "stage": stage, "time": time.time(), "reserved_tokens": reserve, "status": "reserved"}
            self.usage["events"].append(event)
            save(self.path, self.usage)
            save(response_path, {"status": "reserved", "stage": stage, "payload": payload, "instruction": instruction})
        try:
            response = self.client.call_json(stage, payload, instruction)
            if not isinstance(response, dict):
                raise ValueError("Client returned non-object")
        except Exception as exc:
            with self.lock:
                event.update(status="failed", error_type=type(exc).__name__)
                save(response_path, {"status": "failed", "error_type": type(exc).__name__})
                save(self.path, self.usage)
            raise BatchFailed("Request failed: " + type(exc).__name__) from None
        # 请求已成功；后续文件错误不能将已付费响应覆盖为失败标记。
        with self.lock:
            usage = response.get("_usage") or {}
            usage = usage if isinstance(usage, dict) else {}
            actual = usage.get("total_tokens")
            if isinstance(actual, int) and actual >= 0:
                self.usage["charged_tokens"] += actual - reserve
            event.update(status="response", usage=usage, actual_tokens=actual)
            persistence_errors = []
            response_record = {"status": "response", "stage": stage, "payload": payload, "instruction": instruction, "response": response}
            try:
                save(response_path, response_record)
            except OSError as exc:
                persistence_errors.append("response_cache:" + type(exc).__name__)
                recovery_path = response_path.with_suffix(".json.tmp")
                try:
                    retained = json.loads(recovery_path.read_text("utf-8")) == response_record
                except (OSError, ValueError):
                    retained = False
                if not retained:
                    recovery_path = self.root / "persistence_recovery" / (key + ".response.json")
                    try:
                        save(recovery_path, response_record)
                        retained = True
                    except OSError:
                        recovery_path = recovery_path.with_suffix(".json.tmp")
                        try:
                            retained = json.loads(recovery_path.read_text("utf-8")) == response_record
                        except (OSError, ValueError):
                            retained = False
                event.update(status="response_persistence_failed", response_recovery_path=str(recovery_path), raw_response_durable=retained)
                if not retained:
                    persistence_errors.append("raw_response_recovery_unavailable")
            try:
                save(self.path, self.usage)
            except OSError as exc:
                persistence_errors.append("usage:" + type(exc).__name__)
            if persistence_errors:
                raise BatchFailed("API returned successfully but local persistence failed; inspect cache/recovery paths and usage, no API retry: " + ", ".join(persistence_errors), kind="persistence_failure")
        return response

    def call(self, stage, payload, instruction, validator, recovery_validator=None):
        if len((instruction + dumps(payload)).encode("utf-8")) > int(self.config.get("limits", {}).get("max_request_bytes", 60000)):
            raise BatchFailed("Prompt exceeds max_request_bytes", kind="prompt_limit")
        key = digest({"stage": stage, "payload": payload, "instruction": instruction,
                      "fingerprint": self.fingerprint, "llm": self.config.get("llm", {})})
        validated_path = self.root / "validated_cache" / (key + ".json")
        if validated_path.exists():
            result = json.loads(validated_path.read_text("utf-8"))
            validator(result)
            return result
        response = self._request(stage, payload, instruction, key)
        if response.get("_finish_reason") == "length":
            raise BatchFailed("Provider output truncated", kind="truncated")
        clean = {k: v for k, v in response.items() if not k.startswith("_")}
        try:
            validator(clean)
        except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
            repair_payload = {"original_payload": payload, "invalid_response": response.get("_raw_response", clean),
                              "validation_error": str(exc)}
            repaired = self._request(stage + ":repair", repair_payload,
                                     instruction + "\nRepair the invalid response using original evidence only. Return a full replacement JSON object.", digest({"repair_of": key}))
            if repaired.get("_finish_reason") == "length":
                raise BatchFailed("Provider structure-repair output truncated", kind="truncated")
            clean = {k: v for k, v in repaired.items() if not k.startswith("_")}
            try:
                validator(clean)
            except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc2:
                if recovery_validator is None:
                    raise BatchFailed("Structure repair failed: " + str(exc2), kind="validation_failure") from None
                try:
                    recovery_validator(clean)
                    validator(clean)
                except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc3:
                    raise BatchFailed("Structure repair recovery failed: " + str(exc3), kind="validation_failure") from None
        save(validated_path, clean)
        return clean
