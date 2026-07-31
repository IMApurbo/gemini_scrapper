#!/usr/bin/env python3
"""
gemini-scrapper - Gemini Web to OpenAI API proxy.
Author: IMApurbo

Converts Google Gemini's web interface into an OpenAI-compatible API server.
Zero authentication required. Works on any platform (Windows/macOS/Linux).

Usage:
    pip install httpx
    python gemini_scrapper.py [--port 8765] [--config config.json]

Client configuration (Cherry Studio, ChatBox, etc.):
    Base URL: http://localhost:8765/v1
    API Key: (anything or empty)

How it works:
    Sends requests directly to Gemini's public StreamGenerate endpoint.
    The backend does not verify authentication for basic text generation.
    Model selection via MODE_CATEGORY field [79] in the request payload.
    This is NOT a user-tier spoofing attack - the endpoint simply doesn't
    require auth for anonymous access.
"""
import json
import urllib.request
import urllib.parse
import time
import ssl
import sys
import uuid
import re
import os
import hashlib
import argparse
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

__version__ = "1.1.0"

# ─── Tool-call emulation constants ────────────────────────────────────────
# Gemini has no native function calling, so we ask it to signal a tool call
# as plain text using these tags, then parse them back out. XML-style tags
# (rather than the old ```tool_call fenced block) are used because Gemini
# often wraps real code in markdown fences too, which made the old fenced
# format ambiguous to parse reliably.
TOOL_CALL_OPEN = "<tool_call>"
TOOL_CALL_CLOSE = "</tool_call>"
TOOL_CALL_RE = re.compile(
    re.escape(TOOL_CALL_OPEN) + r"\s*(\{.*?\})\s*" + re.escape(TOOL_CALL_CLOSE),
    re.DOTALL | re.IGNORECASE,
)
# Strip stray markdown fences Gemini sometimes wraps the tags in anyway,
# despite being told not to.
CODE_FENCE_RE = re.compile(r"```(?:json|xml|tool_call)?\s*|\s*```")

# ─── Configuration ───────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "port": 8765,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.6-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
    "api_format": "anthropic",  # "anthropic" (default) or "openai"
}

CONFIG = dict(DEFAULT_CONFIG)

# ─── Models ──────────────────────────────────────────────────────────────────
# Mapping from JS source: MODE_CATEGORY enum (028-6eb337387583.js)
#   1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE

MODELS = {
    "gemini-3.6-flash": {
        "mode": 1, "think": 4,
        "desc": "Latest all-around model (Gemini 3.6 Flash)",
    },
    "gemini-3.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Alias for gemini-3.6-flash (backend upgraded)",
    },
    "gemini-3.5-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Deep thinking mode, longest output (~20k chars)",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 4,
        "desc": "Pro model (requires cookie for real routing)",
    },
    "gemini-auto": {
        "mode": 4, "think": 4,
        "desc": "Auto model selection",
    },
    "gemini-3.5-flash-thinking-lite": {
        "mode": 5, "think": 0,
        "desc": "Dynamic thinking with adaptive depth",
    },
    "gemini-flash-lite": {
        "mode": 6, "think": 4,
        "desc": "Lightweight fast model",
    },
}

# ─── Utilities ───────────────────────────────────────────────────────────────

def log(msg: str):
    if CONFIG["log_requests"]:
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def load_cookie() -> tuple:
    """Load cookie from file. Returns (cookie_str, sapisid)."""
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file:
        return "", None
    if not os.path.exists(cookie_file):
        return "", None
    try:
        with open(cookie_file, "r") as f:
            content = f.read().strip()
        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = data.get("cookie", "")
            sapisid = data.get("sapisid", "")
        else:
            cookie_str = content
            pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
            sapisid = pairs.get("SAPISID", "")
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return "", None


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


# ─── Gemini Protocol ─────────────────────────────────────────────────────────

def gemini_stream_generate(prompt: str, model_id: int, think_mode: int) -> str:
    """Send prompt to Gemini StreamGenerate with retry."""
    inner = [None] * 80
    inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [2]
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    body = urllib.parse.urlencode(params).encode()
    reqid = int(time.time()) % 1000000
    prefix = account_prefix()
    url = (
        f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])

    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    last_err = None
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            ctx = ssl.create_default_context()
            proxy = CONFIG.get("proxy")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
            return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err


def gemini_stream_generate_iter(prompt: str, model_id: int, think_mode: int):
    """Send prompt and yield incremental text deltas using httpx streaming."""
    inner = [None] * 80
    inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [2]
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    body = urllib.parse.urlencode(params)
    reqid = int(time.time()) % 1000000
    prefix = account_prefix()
    url = (
        f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    proxy = CONFIG.get("proxy")

    if not HAS_HTTPX:
        # Fallback: non-streaming with urllib
        raw = gemini_stream_generate(prompt, model_id, think_mode)
        text = extract_response_text(raw)
        if text:
            yield text
        return

    prev_text = ""
    transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
    with httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True) as client:
        with client.stream("POST", url, content=body, headers=headers) as resp:
            resp.raise_for_status()
            buf = ""
            for chunk in resp.iter_text():
                buf += chunk
                if "BardErrorInfo" in buf:
                    import re as _re
                    m = _re.search(r'BardErrorInfo\s*\[(\d+)\]', buf)
                    if m:
                        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{m.group(1)}]")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if '"wrb.fr"' not in line or len(line) < 200:
                        continue
                    try:
                        arr = json.loads(line)
                        inner_str = arr[0][2]
                        if not inner_str or len(inner_str) < 50:
                            continue
                        inner2 = json.loads(inner_str)
                        if isinstance(inner2, list) and len(inner2) > 4 and inner2[4]:
                            for part in inner2[4]:
                                if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                                    for t in part[1]:
                                        if isinstance(t, str) and len(t) > len(prev_text):
                                            delta = t[len(prev_text):]
                                            delta = clean_gemini_text(delta, strip=False)
                                            if delta:
                                                yield delta
                                            prev_text = t
                    except (json.JSONDecodeError, IndexError, TypeError):
                        pass


def clean_gemini_text(text: str, strip: bool = True) -> str:
    """Remove internal code execution artifacts."""
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    return text.strip() if strip else text


def extract_response_text(raw: str) -> str:
    """Parse StreamGenerate response to extract final text."""
    import re as _re
    bard_err = _re.search(r'BardErrorInfo\s*\[(\d+)\]', raw)
    if bard_err:
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]")
    texts = []
    for line in raw.split("\n"):
        if '"wrb.fr"' not in line or len(line) < 200:
            continue
        try:
            arr = json.loads(line)
            inner_str = arr[0][2]
            if not inner_str or len(inner_str) < 50:
                continue
            inner = json.loads(inner_str)
            if isinstance(inner, list) and len(inner) > 4 and inner[4]:
                for part in inner[4]:
                    if isinstance(part, list) and len(part) > 1 and part[1]:
                        if isinstance(part[1], list):
                            for t in part[1]:
                                if isinstance(t, str) and len(t) > 0:
                                    texts.append(t)
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
    text = ""
    for t in reversed(texts):
        if t.strip():
            text = t
            break
    return clean_gemini_text(text)


# ─── OpenAI Format Helpers ───────────────────────────────────────────────────

def _compact_tool_line(tool_def: dict) -> str:
    """One short line per tool: name, first sentence of its description, and
    just the required parameter names - not the full multi-paragraph
    description and complete JSON schema some clients (e.g. Claude Code)
    send. Forwarding the full text for every tool buries the tool-call
    format instructions under thousands of tokens of noise, which is why
    Gemini would ignore the format entirely on large tool lists."""
    name = tool_def.get("name", "")
    description = (tool_def.get("description") or "").strip()
    first_sentence = description.split(". ")[0].split("\n")[0].strip()
    if len(first_sentence) > 140:
        first_sentence = first_sentence[:140].rstrip() + "..."

    schema = tool_def.get("parameters", {}) or {}
    props = schema.get("properties", {}) or {}
    required = schema.get("required", [])
    param_bits = [f"{p}:{props.get(p, {}).get('type', 'any')}" for p in required]
    optional = [p for p in props if p not in required]
    params_str = ", ".join(param_bits) if param_bits else "none"
    if optional:
        params_str += f" (optional: {', '.join(optional[:6])})"

    return f"- {name}({params_str}) — {first_sentence}"


def _normalize_tool_def(tool: dict) -> dict:
    """Tools arrive in two shapes depending on the caller: OpenAI-style
    {"type": "function", "function": {name, description, parameters}}, or
    already-flat {name, description, parameters} (from
    anthropic_tools_to_internal). Normalize to the flat form."""
    if tool.get("type") == "function" and "function" in tool:
        fn = tool["function"]
    else:
        fn = tool
    return {
        "name": fn.get("name", tool.get("name", "")),
        "description": fn.get("description", tool.get("description", "")),
        "parameters": fn.get("parameters", tool.get("parameters", {})),
    }


def build_tool_block(tools: list) -> str:
    """Build the tool-call format instructions. Callers append this at the
    very END of the prompt (right before Gemini generates), not the start -
    models attend far more to the tail of a long prompt, and putting this
    up front let it get diluted by everything that follows it."""
    if not tools:
        return ""

    tool_defs = [_normalize_tool_def(t) for t in tools]
    tools_block = "\n".join(_compact_tool_line(t) for t in tool_defs)

    return (
        "---\n"
        "[TOOL-CALL FORMAT - read this last block carefully, it overrides "
        "any other formatting instructions above]\n"
        "You can call exactly one of these tools per turn:\n"
        f"{tools_block}\n\n"
        "To call a tool, your ENTIRE reply must be nothing but:\n"
        f"{TOOL_CALL_OPEN}\n"
        '{"name": "<tool name>", "arguments": {<args>}}\n'
        f"{TOOL_CALL_CLOSE}\n"
        "No explanation, no markdown fences, no text outside the tags, no "
        "other tags or schema of your own invention. If no tool is needed, "
        "reply with plain text instead and skip the tags entirely.\n"
        "Example — User: list files in this directory\n"
        f"{TOOL_CALL_OPEN}\n"
        '{"name": "list_files", "arguments": {"path": "."}}\n'
        f"{TOOL_CALL_CLOSE}"
    )


def messages_to_prompt(messages: list, tools: list = None) -> str:
    """Convert OpenAI-style messages to a prompt string. The tool-call
    format block (if any tools were supplied) is appended at the very end,
    after the full message history, instead of injected at the top."""
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content
                if c.get("type") in ("text", "input_text")
            )
        if role == "system":
            parts.append(f"[System instruction]: {content}")
        elif role == "assistant":
            if msg.get("tool_calls"):
                tc_strs = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    tc_strs.append(
                        f"{TOOL_CALL_OPEN}\n"
                        f'{{"name": "{fn.get("name")}", '
                        f'"arguments": {fn.get("arguments", "{}")}}}\n'
                        f"{TOOL_CALL_CLOSE}"
                    )
                parts.append(f"[Assistant]: {content or ''}\n" + "\n".join(tc_strs))
            else:
                parts.append(f"[Assistant]: {content}")
        elif role == "tool":
            parts.append(f"[Tool result for {msg.get('name', '')}]: {content}")
        else:
            parts.append(content if content else "")

    tool_block = build_tool_block(tools)
    if tool_block:
        parts.append(tool_block)

    return "\n\n".join(p for p in parts if p)


def parse_tool_calls(text: str) -> tuple:
    """Extract <tool_call>{...}</tool_call> blocks. Returns (clean_text, tool_calls_list)."""
    cleaned = CODE_FENCE_RE.sub("", text)
    matches = TOOL_CALL_RE.findall(cleaned)
    tool_calls = []
    for raw_json in matches:
        try:
            data = json.loads(raw_json.strip())
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": data["name"],
                    "arguments": json.dumps(data.get("arguments", {}), ensure_ascii=False),
                },
            })
        except (json.JSONDecodeError, KeyError):
            log(f"tool_call tags found but JSON was invalid: {raw_json!r}")

    if not tool_calls:
        log(f"no tool_call tags found in response: {text[:300]!r}")

    clean = TOOL_CALL_RE.sub("", cleaned).strip()
    return clean, tool_calls


# ─── Anthropic Format Helpers ────────────────────────────────────────────────

def _flatten_text_blocks(content) -> str:
    """Turn a string or list-of-blocks content into a plain text string."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "image":
            parts.append("[Note: Image input not supported in this API. Please describe the image in text.]")
    return " ".join(p for p in parts if p)


def _tool_result_text(content) -> str:
    """tool_result content can be a string or a list of blocks (text/image)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    parts.append("[Note: Image input not supported in this API.]")
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(p for p in parts if p)
    return ""


def anthropic_tools_to_internal(tools: list) -> list:
    """Convert Anthropic tool defs (input_schema) to internal (parameters) form."""
    if not tools:
        return None
    converted = []
    for t in tools:
        converted.append({
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {}),
        })
    return converted


def anthropic_request_to_messages(req: dict):
    """Convert an Anthropic /v1/messages request body into the internal
    messages list (role/content[/tool_calls]) plus converted tools.

    Returns (messages, tools)
    """
    messages = []
    tool_names_by_id = {}

    system = req.get("system")
    if system:
        sys_text = system if isinstance(system, str) else _flatten_text_blocks(system)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

    for msg in req.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "assistant":
            if isinstance(content, list):
                text_acc = ""
                tool_calls = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_acc += block.get("text", "")
                    elif btype == "tool_use":
                        call_id = block.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                        tool_names_by_id[call_id] = block.get("name", "")
                        tool_calls.append({
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                            },
                        })
                m = {"role": "assistant", "content": text_acc}
                if tool_calls:
                    m["tool_calls"] = tool_calls
                messages.append(m)
            else:
                messages.append({"role": "assistant", "content": content})

        elif role == "user":
            if isinstance(content, list):
                has_tool_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                if has_tool_result:
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_result":
                            tool_use_id = block.get("tool_use_id", "")
                            name = tool_names_by_id.get(tool_use_id, tool_use_id)
                            text = _tool_result_text(block.get("content", ""))
                            messages.append({"role": "tool", "name": name, "content": text})
                        elif block.get("type") == "text":
                            if block.get("text"):
                                messages.append({"role": "user", "content": block.get("text", "")})
                else:
                    messages.append({"role": "user", "content": _flatten_text_blocks(content)})
            else:
                messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": role, "content": _flatten_text_blocks(content) if isinstance(content, list) else content})

    tools = anthropic_tools_to_internal(req.get("tools"))
    return messages, tools


def build_content_blocks(text: str, tool_calls: list) -> list:
    """Build Anthropic content blocks from clean text + parsed tool calls."""
    blocks = []
    if text:
        blocks.append({"type": "text", "text": text})
    for tc in (tool_calls or []):
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
            "name": fn.get("name", ""),
            "input": args,
        })
    return blocks


def build_message_response(model_name: str, text: str, tool_calls: list, prompt: str) -> dict:
    """Build a full (non-streaming) Anthropic Messages API response."""
    content = build_content_blocks(text, tool_calls)
    stop_reason = "tool_use" if tool_calls else "end_turn"
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model_name,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": len(prompt) // 4,
            "output_tokens": len(text or "") // 4,
        },
    }


def sse_event(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def stream_text_events(model_name: str, prompt: str, delta_iter):
    """Yield SSE byte chunks for a pure-text streaming response."""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    yield sse_event("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [], "model": model_name, "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": len(prompt) // 4, "output_tokens": 0},
        },
    })
    yield sse_event("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    })

    full_text = ""
    for delta in delta_iter:
        if not delta:
            continue
        full_text += delta
        yield sse_event("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": delta},
        })

    yield sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": len(full_text) // 4},
    })
    yield sse_event("message_stop", {"type": "message_stop"})


def stream_full_response_events(response_obj: dict):
    """Yield SSE byte chunks that deliver an already-fully-generated
    response (used for the tool-call path)."""
    msg_id = response_obj["id"]
    yield sse_event("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [], "model": response_obj["model"], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": response_obj["usage"]["input_tokens"], "output_tokens": 0},
        },
    })

    for i, block in enumerate(response_obj["content"]):
        if block["type"] == "text":
            yield sse_event("content_block_start", {
                "type": "content_block_start", "index": i,
                "content_block": {"type": "text", "text": ""},
            })
            if block["text"]:
                yield sse_event("content_block_delta", {
                    "type": "content_block_delta", "index": i,
                    "delta": {"type": "text_delta", "text": block["text"]},
                })
            yield sse_event("content_block_stop", {"type": "content_block_stop", "index": i})
        elif block["type"] == "tool_use":
            yield sse_event("content_block_start", {
                "type": "content_block_start", "index": i,
                "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}},
            })
            yield sse_event("content_block_delta", {
                "type": "content_block_delta", "index": i,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"], ensure_ascii=False)},
            })
            yield sse_event("content_block_stop", {"type": "content_block_stop", "index": i})

    yield sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": response_obj["stop_reason"], "stop_sequence": None},
        "usage": {"output_tokens": response_obj["usage"]["output_tokens"]},
    })
    yield sse_event("message_stop", {"type": "message_stop"})


# ─── HTTP Handler ────────────────────────────────────────────────────────────

class GeminiHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        client_ip = self.client_address[0] if self.client_address else "-"
        log(f"{client_ip} {fmt % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True
        # Authorization: Bearer <key>
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] in keys:
            return True
        # header keys (OpenAI x-api-key / Google x-goog-api-key)
        for h in ("x-api-key", "x-goog-api-key"):
            if self.headers.get(h, "") in keys:
                return True
        # query param ?key= (Gemini CLI native style)
        if "?" in self.path:
            for pair in self.path.split("?", 1)[1].split("&"):
                if pair.startswith("key=") and pair[4:] in keys:
                    return True
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_HEAD(self):
        # Some clients/health-checks probe with HEAD; BaseHTTPRequestHandler
        # has no default handler for it (-> 501). Answer minimally so those
        # probes don't spam the log with unsupported-method errors.
        path_only = self.path.split("?", 1)[0]
        status = 200 if path_only in ("/", "/v1/models") else 404
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        try:
            if self.path.startswith("/v1") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            path_only = self.path.split("?", 1)[0]
            if path_only == "/v1/models":
                self.send_json({"object": "list", "data": [
                    {"id": n, "object": "model", "created": 1700000000,
                     "owned_by": "google", "description": c["desc"]}
                    for n, c in MODELS.items()
                ]})
            elif path_only.startswith("/v1beta/models"):
                self._handle_google_models_list()
            elif path_only == "/":
                self.send_json({"status": "ok", "version": __version__,
                                "api_format": CONFIG.get("api_format", "anthropic"),
                                "models": list(MODELS.keys())})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"GET error: {e}")

    def do_POST(self):
        try:
            if self.path.startswith("/v1") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            api_format = CONFIG.get("api_format", "anthropic")
            path_only = self.path.split("?", 1)[0]

            if path_only == "/v1/messages":
                if api_format != "anthropic":
                    self.send_json({"error": {"message": "server is running in --openai mode; /v1/messages is disabled"}}, 404)
                    return
                self.handle_anthropic_messages(body)
            elif path_only == "/v1/chat/completions":
                if api_format != "openai":
                    self.send_json({"error": {"message": "server is running in --anthropic mode; use /v1/messages instead"}}, 404)
                    return
                self.handle_chat(body)
            elif path_only == "/v1/responses":
                if api_format != "openai":
                    self.send_json({"error": {"message": "server is running in --anthropic mode; use /v1/messages instead"}}, 404)
                    return
                self.handle_responses(body)
            elif ":generateContent" in path_only:
                self._handle_google_generate(body, stream=False)
            elif ":streamGenerateContent" in path_only:
                self._handle_google_generate(body, stream=True)
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}")
            try:
                self.send_json({"error": {"message": str(e)}}, 500)
            except:
                pass

    def _resolve_model(self, model_name):
        think_override = None
        if "@think=" in model_name:
            model_name, think_str = model_name.rsplit("@think=", 1)
            think_override = int(think_str)
        cfg = MODELS.get(model_name)
        if not cfg:
            return None, None, None, f"Unknown model: {model_name}"
        return model_name, cfg["mode"], (think_override if think_override is not None else cfg["think"]), None

    def _call_gemini(self, prompt, model_id, think_mode, tools):
        text = ""
        raw = None
        attempts = max(1, CONFIG.get("retry_attempts", 1))
        for attempt in range(attempts):
            raw = gemini_stream_generate(prompt, model_id, think_mode)
            text = extract_response_text(raw)
            if text:
                break
            # A successful HTTP round-trip but an empty extraction usually
            # means the batchexecute response was in an odd/partial shape
            # this attempt (parsing is regex/index based, not a real
            # protocol client) - retry rather than surfacing a blank
            # response, which Claude Code shows as a generic error.
            if attempt < attempts - 1:
                log(f"empty extraction on attempt {attempt+1}/{attempts}, retrying")
                time.sleep(CONFIG.get("retry_delay_sec", 1))
        tool_calls = None
        if tools and text:
            text, tool_calls = parse_tool_calls(text)
        return text or "", tool_calls

    def handle_chat(self, body: bytes):
        req = json.loads(body)
        model_name, model_id, think_mode, err = self._resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tools = req.get("tools")
        prompt = messages_to_prompt(req.get("messages", []), tools)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty prompt"}}, 400)
            return

        stream = req.get("stream", False)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        if stream and not tools:
            # True streaming: forward chunks as they arrive
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                for delta_text in gemini_stream_generate_iter(prompt, model_id, think_mode):
                    chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                             "model": model_name, "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                # Final chunk
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"Stream error: {e}")
            return

        # Non-streaming (or tool calling which needs full response)
        try:
            text, tool_calls = self._call_gemini(prompt, model_id, think_mode, tools)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"

        if stream:
            # Stream mode with tools: send as single chunk (need full parse for tool_calls)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model_name, "choices": [{"index": 0, "delta": msg, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_json({
                "id": cid, "object": "chat.completion", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(text)//4,
                          "total_tokens": (len(prompt)+len(text))//4},
            })

    def handle_anthropic_messages(self, body: bytes):
        """Anthropic Messages API: POST /v1/messages"""
        req = json.loads(body)
        model_name, model_id, think_mode, err = self._resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"type": "invalid_request_error", "message": err}}, 400)
            return

        messages, tools = anthropic_request_to_messages(req)
        prompt = messages_to_prompt(messages, tools)
        if not prompt.strip():
            self.send_json({"error": {"type": "invalid_request_error", "message": "empty prompt"}}, 400)
            return

        stream = req.get("stream", False)
        tool_choice = req.get("tool_choice")
        tool_choice_none = isinstance(tool_choice, dict) and tool_choice.get("type") == "none"

        if stream and (not tools or tool_choice_none):
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                for chunk in stream_text_events(
                        model_name, prompt,
                        gemini_stream_generate_iter(prompt, model_id, think_mode)):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"Stream error: {e}")
            return

        try:
            text, tool_calls = self._call_gemini(prompt, model_id, think_mode, tools)
        except Exception as e:
            self.send_json({"error": {"type": "api_error", "message": f"upstream error: {e}"}}, 502)
            return

        if tool_choice_none:
            tool_calls = None

        response_obj = build_message_response(model_name, text, tool_calls, prompt)

        if stream:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                for chunk in stream_full_response_events(response_obj):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_json(response_obj)

    def handle_responses(self, body: bytes):
        """OpenAI Responses API for Codex CLI compatibility."""
        req = json.loads(body)
        model_name, model_id, think_mode, err = self._resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        input_items = req.get("input", [])
        tools = req.get("tools")

        messages = []
        if req.get("instructions"):
            messages.append({"role": "system", "content": req["instructions"]})
        if isinstance(input_items, str):
            messages.append({"role": "user", "content": input_items})
        elif isinstance(input_items, list):
            for item in input_items:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    if item.get("type") == "function_call_output":
                        messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                                         "name": item.get("name", ""), "content": item.get("output", "")})
                    elif item.get("role") == "assistant" or (item.get("type") == "message" and item.get("role") == "assistant"):
                        cp = item.get("content", [])
                        text_acc, tc_list = "", []
                        if isinstance(cp, list):
                            for c in cp:
                                if isinstance(c, dict):
                                    if c.get("type") == "output_text": text_acc += c.get("text", "")
                                    elif c.get("type") == "function_call": tc_list.append(c)
                        elif isinstance(cp, str):
                            text_acc = cp
                        m = {"role": "assistant", "content": text_acc or None}
                        if tc_list:
                            m["tool_calls"] = [{"id": tc.get("call_id", f"call_{i}"), "type": "function",
                                                "function": {"name": tc.get("name",""), "arguments": tc.get("arguments","{}")}}
                                               for i, tc in enumerate(tc_list)]
                        messages.append(m)
                    else:
                        role = item.get("role", "user")
                        content = item.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(c.get("text", "") for c in content if c.get("type") in ("text", "input_text"))
                        messages.append({"role": role, "content": content})

        if tools:
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t for t in tools]

        prompt = messages_to_prompt(messages, tools)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty input"}}, 400)
            return

        try:
            text, tool_calls = self._call_gemini(prompt, model_id, think_mode, tools)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        rid = f"resp_{uuid.uuid4().hex[:16]}"
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        output = []
        if tool_calls:
            for tc in tool_calls:
                output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                               "name": tc["function"]["name"], "arguments": tc["function"]["arguments"], "status": "completed"})
        if text or not tool_calls:
            output.append({"type": "message", "id": mid, "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": text or "", "annotations": []}]})

        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            ev = {"type": "response.created", "response": {"id": rid, "object": "response", "status": "in_progress", "model": model_name, "output": []}}
            self.wfile.write(f"event: response.created\ndata: {json.dumps(ev)}\n\n".encode())
            for item in output:
                if item["type"] == "function_call":
                    ev = {"type": "response.function_call_arguments.done", "item_id": item["id"], "call_id": item["call_id"], "name": item["name"], "arguments": item["arguments"]}
                    self.wfile.write(f"event: response.function_call_arguments.done\ndata: {json.dumps(ev)}\n\n".encode())
                elif item["type"] == "message":
                    for ci, cp in enumerate(item["content"]):
                        ev = {"type": "response.output_text.done", "item_id": item["id"], "content_index": ci, "text": cp["text"]}
                        self.wfile.write(f"event: response.output_text.done\ndata: {json.dumps(ev)}\n\n".encode())
            resp_obj = {"id": rid, "object": "response", "status": "completed", "model": model_name, "output": output,
                        "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text)//4, "total_tokens": (len(prompt)+len(text))//4}}
            self.wfile.write(f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': resp_obj})}\n\n".encode())
            self.wfile.flush()
        else:
            self.send_json({"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
                            "model": model_name, "output": output,
                            "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text)//4, "total_tokens": (len(prompt)+len(text))//4}})


    # ─── Google Native API (Gemini CLI compatible) ────────────────────────────

    def _parse_google_model_from_path(self):
        """Extract model name from /v1beta/models/{model}:method path."""
        m = re.match(r'/v1beta/models/([^:?]+)', self.path)
        if m:
            return m.group(1)
        return None

    def _handle_google_models_list(self):
        """GET /v1beta/models — Google AI format model list."""
        models = []
        for name, cfg in MODELS.items():
            models.append({
                "name": f"models/{name}",
                "displayName": name,
                "description": cfg["desc"],
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            })
        self.send_json({"models": models})

    def _google_contents_to_prompt(self, req: dict) -> str:
        """Convert Google API contents format to prompt string."""
        parts = []
        sys_inst = req.get("systemInstruction")
        if sys_inst:
            sys_parts = sys_inst.get("parts", [])
            sys_text = " ".join(p.get("text", "") for p in sys_parts if p.get("text"))
            if sys_text:
                parts.append(f"[System instruction]: {sys_text}")

        for content in req.get("contents", []):
            role = content.get("role", "user")
            text_parts = []
            for p in content.get("parts", []):
                if p.get("text"):
                    text_parts.append(p["text"])
            text = " ".join(text_parts)
            if role == "model":
                parts.append(f"[Assistant]: {text}")
            else:
                parts.append(text)
        return "\n\n".join(p for p in parts if p)

    def _handle_google_generate(self, body: bytes, stream: bool):
        """Handle Google native generateContent / streamGenerateContent."""
        req = json.loads(body)
        model_name = self._parse_google_model_from_path()
        if not model_name:
            self.send_json({"error": {"message": "model not specified in path"}}, 400)
            return

        model_name, model_id, think_mode, err = self._resolve_model(model_name)
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        prompt = self._google_contents_to_prompt(req)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty content"}}, 400)
            return

        try:
            text, _ = self._call_gemini(prompt, model_id, think_mode, None)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        candidate = {
            "content": {"parts": [{"text": text or ""}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {
            "promptTokenCount": len(prompt) // 4,
            "candidatesTokenCount": len(text) // 4,
            "totalTokenCount": (len(prompt) + len(text)) // 4,
        }
        response_obj = {
            "candidates": [candidate],
            "usageMetadata": usage,
            "modelVersion": model_name,
        }

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps(response_obj)}\n\n".encode())
            self.wfile.flush()
        else:
            self.send_json(response_obj)


# ─── Main ────────────────────────────────────────────────────────────────────

def load_config(path: str):
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
        log(f"Config loaded: {path}")


def main():
    parser = argparse.ArgumentParser(description="Gemini Web to OpenAI API")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None, help="Path to cookie file")
    parser.add_argument("--proxy", type=str, default=None, help="HTTP proxy, e.g. http://127.0.0.1:7890")

    fmt_group = parser.add_mutually_exclusive_group()
    fmt_group.add_argument("--anthropic", action="store_true",
                            help="Serve Anthropic-compatible endpoint (/v1/messages) [default]")
    fmt_group.add_argument("--openai", action="store_true",
                            help="Serve OpenAI-compatible endpoints (/v1/chat/completions, /v1/responses)")

    parser.add_argument("--version", action="version", version=f"gemini-scrapper {__version__}")
    args = parser.parse_args()

    config_path = args.config or os.environ.get("GEMINI_SCRAPPER_CONFIG")
    if not config_path:
        for p in ["./config.json", os.path.expanduser("~/.config/gemini-scrapper/config.json")]:
            if os.path.exists(p):
                config_path = p
                break
    load_config(config_path)

    if args.port:
        CONFIG["port"] = args.port
    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        CONFIG["proxy"] = args.proxy
    if args.openai:
        CONFIG["api_format"] = "openai"
    elif args.anthropic:
        CONFIG["api_format"] = "anthropic"
    # else: keep whatever config.json set, defaulting to "anthropic"

    class ThreadedServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    port = CONFIG["port"]
    server = ThreadedServer((CONFIG["host"], port), GeminiHandler)
    api_format = CONFIG.get("api_format", "anthropic")
    endpoint = "/v1/messages" if api_format == "anthropic" else "/v1/chat/completions (+ /v1/responses)"
    print(f"gemini-scrapper v{__version__}")
    print(f"  Listening: http://0.0.0.0:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Format:    {api_format}  ({endpoint})")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    print(f"  Cookie:    {'yes (' + CONFIG['cookie_file'] + ')' if CONFIG.get('cookie_file') else 'none (anonymous)'}")
    print(f"  Proxy:     {CONFIG.get('proxy') or 'none (uses system env HTTP_PROXY/HTTPS_PROXY)'}")
    print(f"  Retry:     {CONFIG['retry_attempts']}x / {CONFIG['retry_delay_sec']}s")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
