"""
qwen-coder-proxy
================
Sits between any OpenAI-API client (e.g. opencode) and Ollama, intercepting
qwen3_coder XML tool-call output (which Ollama's OpenAI-compat layer cannot
parse natively) and converting it into the OpenAI `tool_calls` field.

Why this exists
---------------
Qwen3.6-35B-A3B emits tool calls in qwen3_coder XML format:

    <tool_call>
    <function=NAME>
    <parameter=KEY>VALUE</parameter>
    </function>
    </tool_call>

Ollama 0.21.x's tool parser only recognises Hermes-style JSON, so the XML
falls through as plain assistant text and clients see no `tool_calls`. This
proxy:

    * Parses the XML on the wire and lifts it into OpenAI `tool_calls`
    * Coerces parameter values per-tool-schema (string params stay strings,
      array/object params get JSON-decoded)
    * Defensively fills missing required parameters (e.g. `description` for
      a bash tool when the model omits it)
    * Splits Qwen3.6's prompt-prefilled `<think>...</think>` reasoning into
      `reasoning_content` so it doesn't pollute the visible chat
    * Streams in real time (SSE forwarding from Ollama, transformed on the fly)

Run
---
    python qwen_coder_proxy.py            # serve (listens on $QWEN_PROXY_HOST:$QWEN_PROXY_PORT)
    python qwen_coder_proxy.py --test     # run unit tests only

Environment variables
---------------------
    QWEN_PROXY_HOST       default "0.0.0.0"
    QWEN_PROXY_PORT       default "18000"
    QWEN_PROXY_UPSTREAM   default "http://127.0.0.1:11434"
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, Response
import uvicorn


UPSTREAM = os.environ.get("QWEN_PROXY_UPSTREAM", "http://127.0.0.1:11434")

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<function=([^>\s]+)>(.*?)</function>\s*</tool_call>",
    re.DOTALL,
)
_PARAM_RE = re.compile(
    r"<parameter=([^>\s]+)>\s*(.*?)\s*</parameter>",
    re.DOTALL,
)


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _coerce_value(raw: str, prop_schema: dict | None = None) -> Any:
    """Coerce a string parameter value based on the schema's declared type.
    Falls back to a conservative heuristic if no schema is given."""
    s = raw.strip()
    schema_type = (prop_schema or {}).get("type")

    # Schema-driven branch — STRING always stays a string (critical: file content
    # for Write/Edit must not be JSON-parsed even if it looks like JSON).
    if schema_type == "string":
        return raw
    if schema_type in ("integer", "number"):
        try:
            return int(s) if schema_type == "integer" else float(s)
        except ValueError:
            return raw
    if schema_type == "boolean":
        if s.lower() in ("true", "false"):
            return s.lower() == "true"
        return raw
    if schema_type in ("array", "object"):
        try:
            return json.loads(s)
        except Exception:
            pass
        # Recovery: model sometimes emits comma-separated objects without the
        # enclosing array brackets, e.g. `{"x":1},{"y":2}` instead of
        # `[{"x":1},{"y":2}]`. If schema says array, try wrapping.
        if schema_type == "array":
            try:
                return json.loads("[" + s + "]")
            except Exception:
                pass
        return raw

    # No schema info: cautious heuristic. Only parse JSON if it looks unambiguously
    # like a structured value AND parses cleanly. NEVER parse for unknown schema —
    # treat as string by default to avoid breaking string-typed params.
    if not s:
        return ""
    return raw


def extract_tool_calls(content: str, tools: list[dict] | None = None) -> tuple[str, list[dict]]:
    """
    Scan content for qwen3_coder XML tool-call blocks. Return:
      (cleaned_content, [openai_tool_calls...])
    cleaned_content has the <tool_call>...</tool_call> blocks removed; thinking
    blocks and other text are preserved.
    If `tools` is provided, parameter values are coerced according to each tool's
    JSON schema (string-typed params remain strings even if they look like JSON).
    """
    schema_by_name: dict[str, dict] = {}
    if tools:
        for t in tools:
            fn = (t or {}).get("function") or {}
            nm = fn.get("name")
            if nm:
                schema_by_name[nm] = (fn.get("parameters") or {}).get("properties") or {}

    calls: list[dict] = []

    def _replace(m: re.Match) -> str:
        fn_name = m.group(1).strip()
        body = m.group(2)
        props = schema_by_name.get(fn_name, {})
        args: dict[str, Any] = {}
        for pm in _PARAM_RE.finditer(body):
            key = pm.group(1).strip()
            args[key] = _coerce_value(pm.group(2), props.get(key))
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:10]}",
            "type": "function",
            "function": {
                "name": fn_name,
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        })
        return ""  # strip the XML from content

    cleaned = _TOOL_CALL_RE.sub(_replace, content)
    # collapse triple+ newlines left behind
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, calls


def split_thinking(content: str) -> tuple[str, str]:
    """
    Qwen3.6's chat template pre-fills `<think>\\n` in the assistant prompt, so
    response content typically starts with thinking text and is closed by
    `</think>` before the real answer. Split into (real_content, reasoning).

    If the content has no `</think>`, returns (content, "") — i.e. assume no
    thinking happened.
    """
    if "</think>" not in content:
        return content, ""
    pre, _, post = content.partition("</think>")
    # pre may also start with a stray `<think>` if the model emitted it (rare,
    # but defensive). Strip it.
    reasoning = pre
    if reasoning.startswith("<think>"):
        reasoning = reasoning[len("<think>"):]
    return post.strip(), reasoning.strip()


# ---------------------------------------------------------------------------
# Response transformation
# ---------------------------------------------------------------------------


def _placeholder_for(name: str, existing_args: dict, prop_schema: dict) -> Any:
    """Generate a safe placeholder for a missing required field."""
    # Special case: `description` paired with `command` (bash-style tools)
    if name == "description" and "command" in existing_args:
        cmd = str(existing_args["command"])[:80]
        return f"Run: {cmd}"
    if name == "description":
        return "(auto-filled by proxy: model omitted required description)"
    # Special case: `priority` on todo-style items — opencode schemas commonly
    # use enum ["low","medium","high"]; "medium" is the safest default.
    if name == "priority":
        enum = prop_schema.get("enum")
        if enum:
            return "medium" if "medium" in enum else enum[0]
        return "medium"
    # enum default if present
    enum = prop_schema.get("enum")
    if enum:
        return enum[0]
    # type-based defaults
    t = prop_schema.get("type")
    if t == "string":
        return ""
    if t in ("integer", "number"):
        return 0
    if t == "boolean":
        return False
    if t == "array":
        return []
    if t == "object":
        return {}
    return ""


def _fill_required_in_items(value: Any, item_schema: dict) -> int:
    """If `value` is a list of objects and `item_schema` declares required
    properties, inject placeholders for missing fields on each item.
    Returns count of fields filled."""
    if not isinstance(value, list) or not isinstance(item_schema, dict):
        return 0
    required = item_schema.get("required") or []
    if not required:
        return 0
    properties = item_schema.get("properties") or {}
    filled = 0
    for elem in value:
        if not isinstance(elem, dict):
            continue
        for r in required:
            if r not in elem:
                elem[r] = _placeholder_for(r, elem, properties.get(r) or {})
                filled += 1
    return filled


def fill_missing_required_args(tool_calls: list[dict], tools: list[dict]) -> int:
    """For each tool call, ensure every required parameter from the tool's
    schema is present in arguments. Inject placeholders for missing ones,
    INCLUDING required fields on items inside array-typed parameters
    (e.g. todos[i].priority). Returns count of fields filled."""
    if not tools:
        return 0
    schema_by_name: dict[str, dict] = {}
    for t in tools:
        fn = t.get("function") or {}
        nm = fn.get("name")
        if nm:
            schema_by_name[nm] = fn.get("parameters") or {}

    filled = 0
    for call in tool_calls:
        fn = call.get("function") or {}
        name = fn.get("name")
        schema = schema_by_name.get(name)
        if not schema:
            continue
        required = schema.get("required") or []
        properties = schema.get("properties") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            continue
        changed = False
        # Top-level required fields
        for r in required:
            if r not in args:
                args[r] = _placeholder_for(r, args, properties.get(r) or {})
                changed = True
                filled += 1
        # Required fields on array items (e.g. todos[i].priority)
        for prop_name, prop_schema in properties.items():
            if not isinstance(prop_schema, dict):
                continue
            if prop_schema.get("type") != "array":
                continue
            n = _fill_required_in_items(args.get(prop_name), prop_schema.get("items") or {})
            if n:
                changed = True
                filled += n
        if changed:
            fn["arguments"] = json.dumps(args, ensure_ascii=False)
    return filled


def transform_response(body: dict, tools: list[dict] | None = None) -> dict:
    """Mutate an OpenAI chat-completion response:
      - Lift qwen3_coder XML tool-calls into message.tool_calls
      - Coerce param values per-schema (string params stay strings)
      - Defensively fill missing required arguments
      - Split <think>...</think> reasoning into message.reasoning_content,
        leaving message.content with only the real reply text
    """
    for choice in body.get("choices", []):
        msg = choice.get("message") or {}
        content = msg.get("content") or ""

        # Step 1: extract tool calls (XML -> structured), schema-aware coercion
        if "<tool_call>" in content:
            cleaned, calls = extract_tool_calls(content, tools=tools)
            if calls:
                content = cleaned
                existing = msg.get("tool_calls") or []
                msg["tool_calls"] = existing + calls
                if tools:
                    fill_missing_required_args(msg["tool_calls"], tools)
                for i, c in enumerate(msg["tool_calls"]):
                    c.setdefault("index", i)
                if choice.get("finish_reason") in (None, "stop"):
                    choice["finish_reason"] = "tool_calls"

        # Step 2: split thinking out of content
        real_content, reasoning = split_thinking(content)
        msg["content"] = real_content if real_content else None
        if reasoning:
            # OpenAI extension also used by DeepSeek; AI SDK openai-compatible
            # routes this into a structured reasoning part for clients.
            msg["reasoning_content"] = reasoning
            msg["reasoning"] = reasoning

        choice["message"] = msg
    return body


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def _safe_tail_keep(buffer: str) -> int:
    """Return how many trailing chars of `buffer` we should HOLD BACK from
    emitting as content because they might be the start of `<tool_call>` or
    `</think>` (we don't yet know if they'll grow into the full marker)."""
    needles = ("<tool_call>", "</think>")
    max_keep = max(len(n) for n in needles) - 1
    if len(buffer) <= max_keep:
        for n in needles:
            for k in range(1, min(len(buffer), len(n)) + 1):
                if buffer.endswith(n[:k]):
                    return k
        return 0
    tail = buffer[-max_keep:]
    keep = 0
    for n in needles:
        for k in range(1, min(len(tail), len(n) - 1) + 1):
            if tail.endswith(n[:k]):
                keep = max(keep, k)
    return keep


async def _stream_chat_completion(client_body: dict, headers: dict, tools: list[dict]):
    """
    Open a streaming connection to upstream Ollama, transform the SSE on the fly:
      - Initial output is reasoning (Qwen3.6 prompt prefills <think>\\n) — emit as
        delta.reasoning_content until we see </think>
      - After </think>, emit as delta.content
      - When a complete <tool_call>...</tool_call> block accumulates, parse it
        into delta.tool_calls and remove from the content stream
    """
    upstream_body = dict(client_body)
    upstream_body["stream"] = True
    upstream_body["stream_options"] = {"include_usage": True}

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model = client_body.get("model", "unknown")

    in_thinking = True
    pending = ""
    tool_count = 0
    finish_reason = "stop"
    final_usage: dict | None = None

    def make_chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> bytes:
        c = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage is not None:
            c["usage"] = usage
        return b"data: " + json.dumps(c).encode("utf-8") + b"\n\n"

    yield make_chunk({"role": "assistant"})

    try:
        async with _client.stream(
            "POST", "/v1/chat/completions",
            content=json.dumps(upstream_body).encode("utf-8"),
            headers={**headers, "content-type": "application/json"},
        ) as upstream:
            if upstream.status_code != 200:
                err = await upstream.aread()
                yield b"data: " + err + b"\n\n"
                yield b"data: [DONE]\n\n"
                return
            async for line in upstream.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except Exception:
                    continue
                if chunk.get("usage"):
                    final_usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                fr = choices[0].get("finish_reason")
                if fr and finish_reason != "tool_calls":
                    finish_reason = fr
                piece = delta.get("content") or ""
                if not piece:
                    continue

                pending += piece

                made_progress = True
                while made_progress:
                    made_progress = False

                    if in_thinking:
                        idx = pending.find("</think>")
                        if idx != -1:
                            reasoning_text = pending[:idx]
                            if reasoning_text:
                                yield make_chunk({"reasoning_content": reasoning_text,
                                                  "reasoning": reasoning_text})
                            pending = pending[idx + len("</think>"):].lstrip("\n")
                            in_thinking = False
                            made_progress = True
                            continue
                        keep = _safe_tail_keep(pending)
                        emit_reason = pending[:len(pending) - keep] if keep else pending
                        if emit_reason:
                            yield make_chunk({"reasoning_content": emit_reason,
                                              "reasoning": emit_reason})
                            pending = pending[len(emit_reason):]
                        continue

                    tc_start = pending.find("<tool_call>")
                    if tc_start != -1:
                        if tc_start > 0:
                            before = re.sub(r"\n{3,}", "\n\n", pending[:tc_start])
                            if before.strip():
                                yield make_chunk({"content": before})
                            pending = pending[tc_start:]
                        tc_end = pending.find("</tool_call>")
                        if tc_end == -1:
                            break
                        full_tc = pending[: tc_end + len("</tool_call>")]
                        _, calls = extract_tool_calls(full_tc, tools=tools)
                        if calls:
                            if tools:
                                fill_missing_required_args(calls, tools)
                            for c in calls:
                                c["index"] = tool_count
                                tool_count += 1
                                yield make_chunk({"tool_calls": [c]})
                        pending = pending[tc_end + len("</tool_call>"):]
                        finish_reason = "tool_calls"
                        made_progress = True
                        continue

                    keep = _safe_tail_keep(pending)
                    emit_content = pending[:len(pending) - keep] if keep else pending
                    if emit_content:
                        yield make_chunk({"content": emit_content})
                        pending = pending[len(emit_content):]

            if pending:
                if in_thinking:
                    yield make_chunk({"reasoning_content": pending,
                                      "reasoning": pending})
                else:
                    tool_match = _TOOL_CALL_RE.search(pending)
                    if tool_match:
                        before = pending[:tool_match.start()]
                        if before.strip():
                            yield make_chunk({"content": before})
                        _, calls = extract_tool_calls(
                            pending[tool_match.start():tool_match.end()], tools=tools)
                        if calls:
                            if tools:
                                fill_missing_required_args(calls, tools)
                            for c in calls:
                                c["index"] = tool_count
                                tool_count += 1
                                yield make_chunk({"tool_calls": [c]})
                        finish_reason = "tool_calls"
                    else:
                        if pending.strip():
                            yield make_chunk({"content": pending})

            yield make_chunk({}, finish=finish_reason, usage=final_usage)
            yield b"data: [DONE]\n\n"
    except Exception as e:
        err = json.dumps({"error": {"message": f"proxy stream error: {e}"}})
        yield b"data: " + err.encode() + b"\n\n"
        yield b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="qwen-coder-proxy", version="0.2.0")
_client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _startup():
    global _client
    _client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600.0))


@app.on_event("shutdown")
async def _shutdown():
    if _client:
        await _client.aclose()


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy(path: str, request: Request):
    method = request.method
    raw_body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "transfer-encoding")}

    is_chat = path == "v1/chat/completions" and method == "POST"

    if is_chat:
        try:
            client_body = json.loads(raw_body or b"{}")
        except Exception:
            return JSONResponse(status_code=400, content={"error": "bad JSON"})
        wants_stream = bool(client_body.get("stream"))
        client_tools = client_body.get("tools") or []

        if wants_stream:
            return StreamingResponse(
                _stream_chat_completion(client_body, headers, client_tools),
                media_type="text/event-stream",
            )

        upstream_body = dict(client_body)
        upstream_body["stream"] = False
        r = await _client.request(
            method, "/" + path,
            content=json.dumps(upstream_body).encode("utf-8"),
            headers={**headers, "content-type": "application/json"},
        )
        if r.status_code != 200:
            return Response(content=r.content, status_code=r.status_code,
                            media_type=r.headers.get("content-type", "application/json"))
        try:
            j = r.json()
        except Exception:
            return Response(content=r.content, status_code=200,
                            media_type=r.headers.get("content-type", "application/json"))
        j = transform_response(j, tools=client_tools)
        return JSONResponse(content=j)

    # passthrough for everything else (models list, embeddings, etc.)
    r = await _client.request(method, "/" + path, content=raw_body, headers=headers,
                              params=request.query_params)
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/octet-stream"),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _run_tests():
    failures = 0

    def assert_eq(name, got, want):
        nonlocal failures
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
        if not ok:
            failures += 1
            print(f"    got:  {got!r}")
            print(f"    want: {want!r}")

    print("Test 1: plain content, no tool call")
    cleaned, calls = extract_tool_calls("Hello world")
    assert_eq("content unchanged", cleaned, "Hello world")
    assert_eq("no calls", calls, [])

    print("\nTest 2: single tool call, simple")
    raw = ("Some prefatory thinking text.\n\n"
           "<tool_call>\n<function=read>\n"
           "<parameter=file_path>\n/tmp/foo.txt\n</parameter>\n"
           "</function>\n</tool_call>")
    cleaned, calls = extract_tool_calls(raw)
    assert_eq("content stripped", cleaned, "Some prefatory thinking text.")
    assert_eq("one call", len(calls), 1)
    assert_eq("name", calls[0]["function"]["name"], "read")
    args = json.loads(calls[0]["function"]["arguments"])
    assert_eq("args", args, {"file_path": "/tmp/foo.txt"})

    print("\nTest 3: multi-param tool call, multi-line value")
    raw = ("<tool_call>\n<function=write>\n"
           "<parameter=path>\n/tmp/x.txt\n</parameter>\n"
           "<parameter=content>\nline1\nline2\nline3\n</parameter>\n"
           "</function>\n</tool_call>")
    _, calls = extract_tool_calls(raw)
    assert_eq("one call", len(calls), 1)
    args = json.loads(calls[0]["function"]["arguments"])
    assert_eq("path", args["path"], "/tmp/x.txt")
    assert_eq("content multiline", args["content"], "line1\nline2\nline3")

    print("\nTest 4: schema-aware coercion (integer-typed param coerces)")
    raw = ("<tool_call>\n<function=a>\n<parameter=x>\n1\n</parameter>\n</function>\n</tool_call>\n"
           "<tool_call>\n<function=b>\n<parameter=y>\n2\n</parameter>\n</function>\n</tool_call>")
    schema_tools = [
        {"type":"function","function":{"name":"a","parameters":{"type":"object",
            "properties":{"x":{"type":"integer"}},"required":["x"]}}},
        {"type":"function","function":{"name":"b","parameters":{"type":"object",
            "properties":{"y":{"type":"integer"}},"required":["y"]}}},
    ]
    _, calls = extract_tool_calls(raw, tools=schema_tools)
    assert_eq("two calls", len(calls), 2)
    assert_eq("integer-typed param coerced", json.loads(calls[0]["function"]["arguments"])["x"], 1)

    print("\nTest 5: thinking preserved, tool call stripped")
    raw = ("<think>\nReasoning here\n</think>\n\n"
           "<tool_call>\n<function=read>\n<parameter=file_path>\n/x\n</parameter>\n</function>\n</tool_call>")
    cleaned, calls = extract_tool_calls(raw)
    assert "<think>" in cleaned, f"thinking should be preserved, got {cleaned!r}"
    assert "<tool_call>" not in cleaned, f"tool call should be stripped, got {cleaned!r}"
    assert_eq("one call", len(calls), 1)
    print("  PASS thinking preserved + tool call stripped")

    print("\nTest 6: string-typed param stays a string even if it looks like JSON")
    raw = ('<tool_call>\n<function=write>\n<parameter=path>\n/tmp/x.json\n</parameter>\n'
           '<parameter=content>\n{"foo": "bar", "n": 5}\n</parameter>\n'
           '</function>\n</tool_call>')
    write_tools = [{"type":"function","function":{"name":"write","parameters":{
        "type":"object",
        "properties":{"path":{"type":"string"},"content":{"type":"string"}},
        "required":["path","content"]}}}]
    _, calls = extract_tool_calls(raw, tools=write_tools)
    args = json.loads(calls[0]["function"]["arguments"])
    assert_eq("content stays string", args["content"], '{"foo": "bar", "n": 5}')

    print("\nTest 6b: array-typed param missing enclosing brackets is recovered")
    raw = ('<tool_call>\n<function=todowrite>\n<parameter=todos>\n'
           '{"content":"a","status":"done"},{"content":"b","status":"pending"}\n'
           '</parameter>\n</function>\n</tool_call>')
    todo_tools = [{"type":"function","function":{"name":"todowrite","parameters":{
        "type":"object",
        "properties":{"todos":{"type":"array","items":{"type":"object"}}},
        "required":["todos"]}}}]
    _, calls = extract_tool_calls(raw, tools=todo_tools)
    args = json.loads(calls[0]["function"]["arguments"])
    todos = args["todos"]
    assert_eq("todos is a list", isinstance(todos, list), True)
    assert_eq("two items", len(todos), 2)
    assert_eq("first item is dict", isinstance(todos[0], dict), True)

    print("\nTest 6c: nested array items get missing required fields filled (todos[i].priority)")
    calls_d = [{"function": {"name": "todowrite", "arguments": json.dumps({
        "todos": [
            {"content": "task 1", "status": "completed"},  # missing priority
            {"content": "task 2", "status": "pending", "priority": "high"},
        ]
    })}}]
    todo_full_tools = [{"type":"function","function":{"name":"todowrite","parameters":{
        "type":"object",
        "properties":{"todos":{"type":"array","items":{
            "type":"object",
            "properties":{
                "content":{"type":"string"},
                "status":{"type":"string"},
                "priority":{"type":"string","enum":["low","medium","high"]},
            },
            "required":["content","status","priority"],
        }}},
        "required":["todos"],
    }}}]
    n = fill_missing_required_args(calls_d, todo_full_tools)
    args_d = json.loads(calls_d[0]["function"]["arguments"])
    assert_eq("filled count", n, 1)
    assert_eq("priority filled on item 0", args_d["todos"][0].get("priority"), "medium")
    assert_eq("item 1 unchanged", args_d["todos"][1].get("priority"), "high")

    print("\nTest 7: model-omits-description, proxy fills it from command")
    raw = ("<tool_call>\n<function=bash>\n<parameter=command>\nls /tmp\n</parameter>\n</function>\n</tool_call>")
    _, calls = extract_tool_calls(raw)
    bash_tools = [{"type":"function","function":{"name":"bash","parameters":{
        "type":"object",
        "properties":{"command":{"type":"string"},"description":{"type":"string"}},
        "required":["command","description"]}}}]
    n = fill_missing_required_args(calls, bash_tools)
    args = json.loads(calls[0]["function"]["arguments"])
    assert_eq("filled count", n, 1)
    assert_eq("description references command", "ls /tmp" in args["description"], True)
    assert_eq("command preserved", args["command"], "ls /tmp")

    print("\nTest 8: transform_response on a full response body")
    body = {
        "id": "chatcmpl-x", "model": "local-coder:35b-a3b",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": ("thoughts go here\n</think>\n\n"
                            "<tool_call>\n<function=ls>\n<parameter=dir>\n/tmp\n</parameter>\n</function>\n</tool_call>"),
            },
            "finish_reason": "stop",
        }],
    }
    out = transform_response(body)
    msg = out["choices"][0]["message"]
    assert_eq("tool_calls present", bool(msg.get("tool_calls")), True)
    assert_eq("finish_reason updated", out["choices"][0]["finish_reason"], "tool_calls")
    assert "<tool_call>" not in (msg.get("content") or ""), "content should not contain raw XML"
    assert "</think>" not in (msg.get("content") or ""), "content should not contain </think>"
    assert msg.get("reasoning_content") and "thoughts go here" in msg["reasoning_content"]
    print("  PASS </think> stripped, reasoning extracted")

    print("\nTest 9: split_thinking — Qwen3.6 prompt-prefixed think mode")
    real, reason = split_thinking("internal reasoning here\nstep 2\n</think>\n\nThe answer is 4.")
    assert_eq("real content", real, "The answer is 4.")
    assert_eq("reason content", reason, "internal reasoning here\nstep 2")

    print()
    if failures:
        print(f"=== {failures} FAILURES ===")
        sys.exit(1)
    print("=== all tests passed ===")


if __name__ == "__main__":
    if "--test" in sys.argv:
        _run_tests()
    else:
        port = int(os.environ.get("QWEN_PROXY_PORT", "18000"))
        host = os.environ.get("QWEN_PROXY_HOST", "0.0.0.0")
        print(f"qwen-coder-proxy listening on {host}:{port} -> {UPSTREAM}")
        uvicorn.run(app, host=host, port=port, log_level="info")
