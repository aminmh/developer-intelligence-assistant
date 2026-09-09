"""Read, normalize, filter, and clear developer diagnostic logs."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from infra.settings import settings

LOG_TYPES = ("llm_prompt", "exception", "router_fallback")
_FILES = {
    "llm_prompt": "llm-prompts.log",
    "exception": "exceptions.log",
    "router_fallback": "router-fallback.log",
}
_EXCEPTION_RE = re.compile(r"^\[(?P<timestamp>[^]]+)\]\s+(?P<level>\w+)\s+(?P<thread>[^:]+):\s?(?P<message>.*)$")
_PROMPT_TS_RE = re.compile(r"^\[(?P<timestamp>[^]]+)\]$")
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_EVENTS = 5000


def _parse_timestamp(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return datetime.fromtimestamp(0, tz=timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc).isoformat()


def _event(log_type: str, source: str, offset: int, timestamp: str | None,
           severity: str, title: str, summary: str, details: dict[str, Any], raw: str) -> dict[str, Any]:
    digest = hashlib.sha1(f"{log_type}:{source}:{offset}:{raw}".encode("utf-8")).hexdigest()
    return {
        "id": digest[:20],
        "type": log_type,
        "timestamp": _parse_timestamp(timestamp),
        "severity": severity,
        "title": title,
        "summary": summary[:500],
        "details": details,
    }


def _read(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _MAX_FILE_BYTES))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _parse_prompts(path: Path) -> list[dict[str, Any]]:
    text = _read(path)
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        match = _PROMPT_TS_RE.match(stripped)
        if match:
            if current:
                events.append(_finish_prompt(current))
            current = {"timestamp": match.group("timestamp"), "query": "", "prompt": "", "offset": offset, "in_prompt": False}
        elif current is not None and stripped.startswith("USER_QUERY:"):
            current["query"] = stripped.partition(":")[2].strip()
        elif current is not None and stripped == "FINAL_PROMPT:":
            current["in_prompt"] = True
        elif current is not None and stripped.startswith("-") and len(stripped) >= 20:
            current["in_prompt"] = False
        elif current is not None and current["in_prompt"]:
            current["prompt"] += line
        offset += len(line.encode("utf-8"))
    if current:
        events.append(_finish_prompt(current))
    return events


def _finish_prompt(item: dict[str, Any]) -> dict[str, Any]:
    query = str(item.get("query") or "")
    prompt = str(item.get("prompt") or "").rstrip()
    return _event("llm_prompt", _FILES["llm_prompt"], int(item["offset"]), item.get("timestamp"),
                  "info", "LLM prompt", query or "Prompt sent to the language model",
                  {"query": query, "prompt": prompt}, f"{item.get('timestamp')}:{query}:{prompt}")


def _parse_exceptions(path: Path, source: str) -> list[dict[str, Any]]:
    text = _read(path)
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        match = _EXCEPTION_RE.match(stripped)
        if match:
            if current:
                events.append(_finish_exception(current, source))
            current = {"timestamp": match.group("timestamp"), "level": match.group("level").lower(),
                       "thread": match.group("thread").strip(), "message": match.group("message"),
                       "trace": [], "offset": offset}
        elif current is not None:
            current["trace"].append(stripped)
        offset += len(line.encode("utf-8"))
    if current:
        events.append(_finish_exception(current, source))
    return events


def _finish_exception(item: dict[str, Any], source: str) -> dict[str, Any]:
    level = str(item.get("level") or "error")
    severity = "warning" if level in {"warning", "warn"} else "error"
    message = str(item.get("message") or "Exception")
    trace = "\n".join(item.get("trace") or []).strip()
    return _event("exception", source, int(item["offset"]), item.get("timestamp"), severity,
                  "Exception", message,
                  {"message": message, "thread": item.get("thread"), "stackTrace": trace},
                  f"{item.get('timestamp')}:{message}:{trace}")


def _parse_router(path: Path) -> list[dict[str, Any]]:
    text = _read(path)
    events: list[dict[str, Any]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        raw = line.strip()
        if raw:
            try:
                data = json.loads(raw)
                final_choice = data.get("final_choice") or data.get("embedding_route") or "unknown"
                reason = data.get("reason") or "Router fallback invoked"
                events.append(_event("router_fallback", _FILES["router_fallback"], offset, data.get("timestamp"),
                                     "warning", "Router fallback", f"Selected {final_choice}: {reason}",
                                     {"query": data.get("query"), "route": data.get("embedding_route"),
                                      "finalChoice": final_choice, "reason": reason,
                                      "confidence": data.get("final_confidence"), "metadata": data}, raw))
            except (TypeError, ValueError):
                events.append(_event("router_fallback", _FILES["router_fallback"], offset, None,
                                     "warning", "Malformed router fallback", raw[:500],
                                     {"raw": raw[:2000], "parserWarning": "Invalid JSON log entry"}, raw))
        offset += len(line.encode("utf-8"))
    return events


def _all_events() -> list[dict[str, Any]]:
    root = Path(settings.log_dir)
    events = _parse_prompts(root / _FILES["llm_prompt"])
    events.extend(_parse_router(root / _FILES["router_fallback"]))
    paths = [root / _FILES["exception"], *sorted(root.glob("exceptions.log.[1-9]*"), reverse=True)]
    for path in paths:
        events.extend(_parse_exceptions(path, path.name))
    events.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    return events[:_MAX_EVENTS]


def list_logs(*, search: str = "", types: Iterable[str] | None = None, severity: str = "",
              start: str = "", end: str = "", offset: int = 0, limit: int = 50) -> dict[str, Any]:
    selected = set(types or LOG_TYPES)
    invalid = selected - set(LOG_TYPES)
    if invalid:
        raise ValueError(f"Unknown log type(s): {', '.join(sorted(invalid))}")
    needle = search.casefold().strip()
    start_value = _parse_timestamp(start) if start else ""
    end_value = _parse_timestamp(end) if end else ""
    filtered = []
    for event in _all_events():
        if event["type"] not in selected or (severity and event["severity"] != severity):
            continue
        if start_value and event["timestamp"] < start_value:
            continue
        if end_value and event["timestamp"] > end_value:
            continue
        if needle and needle not in json.dumps(event, ensure_ascii=False).casefold():
            continue
        filtered.append(event)
    safe_offset = max(0, int(offset))
    safe_limit = min(200, max(1, int(limit)))
    return {"events": filtered[safe_offset:safe_offset + safe_limit], "total": len(filtered),
            "offset": safe_offset, "limit": safe_limit,
            "has_more": safe_offset + safe_limit < len(filtered),
            "deletion_mode": settings.log_deletion_mode}


def _paths(log_type: str) -> list[Path]:
    if log_type not in LOG_TYPES:
        raise ValueError(f"Unknown log type: {log_type}")
    root = Path(settings.log_dir)
    active = root / _FILES[log_type]
    return [active, *sorted(root.glob("exceptions.log.[1-9]*"))] if log_type == "exception" else [active]


def clear_logs(log_type: str | None = None, mode: str | None = None) -> dict[str, Any]:
    selected = list(LOG_TYPES) if log_type is None else [log_type]
    selected_mode = mode or settings.log_deletion_mode
    if any(item not in LOG_TYPES for item in selected):
        raise ValueError("Unknown log type")
    if selected_mode not in {"soft", "hard"}:
        raise ValueError("Deletion mode must be soft or hard")
    files: list[str] = []
    reset_exception_logger = False
    for item in selected:
        targets = _paths(item) if selected_mode == "hard" else _paths(item)[:1]
        for path in targets:
            try:
                if not path.exists():
                    continue
                if selected_mode == "hard":
                    path.unlink()
                    if item == "exception":
                        reset_exception_logger = True
                else:
                    path.write_text("", encoding="utf-8")
                files.append(path.name)
            except OSError:
                continue
    if reset_exception_logger:
        logger = logging.getLogger("exceptions")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
    return {"mode": selected_mode, "types": selected, "files": files, "file_count": len(files)}
