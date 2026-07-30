"""Anthropic Messages API (/v1/messages) compatibility layer.

Converts Anthropic-style requests into the internal (role, content) message
list already understood by `tools.messages_to_prompt`, and converts the
internal generation result back into Anthropic-style response/SSE payloads.
"""
import json
import uuid

from .tools import messages_to_prompt, parse_tool_calls


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


def anthropic_tool_choice_to_internal(tool_choice):
    """Convert Anthropic tool_choice to the internal string/dict form."""
    if not tool_choice:
        return "auto"
    ttype = tool_choice.get("type") if isinstance(tool_choice, dict) else None
    if ttype == "none":
        return "none"
    if ttype == "any":
        return "required"
    if ttype == "tool":
        name = tool_choice.get("name", "")
        return {"type": "function", "function": {"name": name}}
    return "auto"


def anthropic_request_to_messages(req: dict):
    """Convert an Anthropic /v1/messages request body into the internal
    messages list (role/content[/tool_calls]) plus converted tools/tool_choice.

    Returns (messages, tools, tool_choice)
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
    tool_choice = anthropic_tool_choice_to_internal(req.get("tool_choice"))
    return messages, tools, tool_choice


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
    """Yield SSE byte chunks for a pure-text streaming response.

    delta_iter yields text deltas from generate_stream(); this generator
    yields fully-formed `event: ...\\ndata: ...\\n\\n` byte strings, and
    finally returns the full accumulated text via StopIteration-adjacent
    bookkeeping is avoided by yielding a final sentinel tuple instead.
    """
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
    response (used for the tool-call path, where we don't have true
    incremental deltas). Mirrors the Anthropic event sequence but sends
    each content block's full text/input in one delta.
    """
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
