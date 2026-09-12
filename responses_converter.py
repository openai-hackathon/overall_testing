#!/usr/bin/env python3
"""
responses_converter.py

Thin proxy: OpenAI Responses API → Chat Completions API

Accepts  POST /v1/responses  (what Codex sends)
Converts to  POST /v1/chat/completions  (what vLLM/Modal accepts)
Translates the SSE stream back into Responses API events.

Usage:
    python3 responses_converter.py --port 60002 --upstream http://127.0.0.1:30000/v1
"""

import argparse
import json
import time
import uuid

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

UPSTREAM = "http://127.0.0.1:30000/v1"

app = FastAPI()


# ---------------------------------------------------------------------------
# Request conversion: Responses API → Chat Completions
# ---------------------------------------------------------------------------

def req_to_chat(body: dict) -> dict:
    messages = []

    # Hard limits to stay within vLLM max_model_len=4096.
    # Total budget: ~3500 tokens (~14000 chars) leaving room for response.
    # Individual caps prevent any single message from dominating.
    MAX_SYSTEM_CHARS = 3000   # ~750 tokens per system message
    MAX_USER_CHARS   = 2000   # ~500 tokens per user message

    if sys_prompt := body.get("instructions"):
        if len(sys_prompt) > MAX_SYSTEM_CHARS:
            sys_prompt = sys_prompt[:MAX_SYSTEM_CHARS] + "\n[truncated]"
        messages.append({"role": "system", "content": sys_prompt})

    raw = body.get("input", "")
    if isinstance(raw, str):
        messages.append({"role": "user", "content": raw})
    elif isinstance(raw, list):
        for item in raw:
            role = item.get("role", "user")
            # "developer" is an OpenAI-internal role; vLLM only accepts "system"
            if role == "developer":
                role = "system"
            content = item.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") in ("input_text", "text")
                )
            limit = MAX_SYSTEM_CHARS if role == "system" else MAX_USER_CHARS
            if len(content) > limit:
                content = content[:limit] + "\n[truncated]"
            messages.append({"role": role, "content": content})

    out: dict = {
        "model": body.get("model", "local"),
        "messages": messages,
        "stream": body.get("stream", False),
        # Required by Modal's Qwen3 deployment to suppress chain-of-thought
        "chat_template_kwargs": {"enable_thinking": False},
    }

    if (v := body.get("max_output_tokens")) is not None:
        out["max_tokens"] = v
    if (v := body.get("temperature")) is not None:
        out["temperature"] = v
    if "priority" in body:
        out["priority"] = body["priority"]

    return out


# ---------------------------------------------------------------------------
# Response conversion: Chat Completions SSE → Responses API SSE
# ---------------------------------------------------------------------------

async def chat_stream_to_responses(resp, resp_id: str, msg_id: str, model: str, ts: int):
    if resp.status_code != 200:
        body = await resp.aread()
        print(f"[converter] upstream error {resp.status_code}: {body.decode(errors='replace')[:500]}")
        err_msg = f"Upstream error {resp.status_code}: {body.decode(errors='replace')[:200]}"
        yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'status': 'failed', 'model': model, 'output': [{'type': 'message', 'id': msg_id, 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': err_msg}]}], 'created_at': ts}})}\n\n"
        yield "data: [DONE]\n\n"
        return

    yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'status': 'in_progress', 'model': model, 'output': [], 'created_at': ts}})}\n\n"

    # Emit output_item.added so Codex registers the item before text deltas arrive
    yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'status': 'in_progress'}})}\n\n"

    # Emit content_part.added so Codex registers the text part
    yield f"data: {json.dumps({'type': 'response.content_part.added', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})}\n\n"

    full = ""
    # aiter_lines() buffers across chunk boundaries so JSON is never split
    async for line in resp.aiter_lines():
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload.strip() == "[DONE]":
            break
        try:
            obj = json.loads(payload)
            delta = (obj.get("choices") or [{}])[0].get("delta", {})
            if text := delta.get("content"):
                full += text
                yield f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': text})}\n\n"
        except Exception:
            pass

    print(f"[converter] streamed {len(full)} chars: {full[:80]!r}")

    yield f"data: {json.dumps({'type': 'response.output_text.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'text': full})}\n\n"
    yield f"data: {json.dumps({'type': 'response.content_part.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': full}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': full}]}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'status': 'completed', 'model': model, 'output': [{'type': 'message', 'id': msg_id, 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': full}]}], 'created_at': ts}})}\n\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def models(request: Request):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{UPSTREAM}/models")
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/v1/responses")
async def responses(request: Request):
    body = await request.json()
    raw = json.dumps(body)
    print(f"[converter] incoming body keys: {list(body.keys())}")
    print(f"[converter] body total chars: {len(raw)}")
    if isinstance(body.get("input"), list):
        for i, msg in enumerate(body["input"]):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            clen = len(content) if isinstance(content, str) else sum(len(p.get("text","")) for p in content if isinstance(p, dict))
            print(f"[converter]   input[{i}] role={role} content_len={clen}")
    if body.get("instructions"):
        print(f"[converter] instructions len: {len(body['instructions'])}")
    chat = req_to_chat(body)
    print(f"[converter] chat messages: {[(m['role'], len(m['content'])) for m in chat['messages']]}")
    resp_id = f"resp-{uuid.uuid4().hex[:16]}"
    msg_id  = f"msg-{uuid.uuid4().hex[:16]}"
    ts      = int(time.time())
    model   = body.get("model", "local")

    if chat.get("stream"):
        async def gen():
            async with httpx.AsyncClient(timeout=120) as c:
                async with c.stream(
                    "POST", f"{UPSTREAM}/chat/completions", json=chat
                ) as r:
                    async for chunk in chat_stream_to_responses(
                        r, resp_id, msg_id, model, ts
                    ):
                        yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    # Non-streaming
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{UPSTREAM}/chat/completions", json=chat)
    data = r.json()
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return {
        "id": resp_id,
        "object": "response",
        "created_at": ts,
        "model": model,
        "status": "completed",
        "output": [{
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text}],
        }],
        "usage": data.get("usage"),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global UPSTREAM
    p = argparse.ArgumentParser(description="Responses API → Chat Completions converter")
    p.add_argument("--port",     type=int, default=60002)
    p.add_argument("--upstream", default="http://127.0.0.1:30000/v1",
                   help="Chat Completions upstream (the vLLM router)")
    args = p.parse_args()
    UPSTREAM = args.upstream
    print(f"responses-converter  127.0.0.1:{args.port}  →  {UPSTREAM}")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
