import json
import httpx
import sqlite3
import os
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=[""], allow_methods=[""], allow_headers=["*"])

PB_URL = "http://127.0.0.1:8090/api/collections/transmissions/records"
LLAMA_URL = "http://127.0.0.1:8080/v1/chat/completions"
DB_PATH = os.path.expanduser("~/Code/.empirica/sessions/sessions.db")

def ensure_payload_integrity(payload: dict) -> dict:
if not isinstance(payload, dict):
payload = {}
if "messages" not in payload:
content = payload.get("query") or payload.get("prompt") or "System: Integrity Check"
payload["messages"] = [{"role": "user", "content": str(content)}]
if not isinstance(payload["messages"], list) or len(payload["messages"]) == 0:
payload["messages"] = [{"role": "user", "content": "System: Empty Payload Recovery"}]
return payload

async def stream_and_capture(payload, persona_id="persona_fogsift"):
payload = ensure_payload_integrity(payload)
try:
last_msg = payload.get("messages", [{}])[-1]
prompt = last_msg.get("content", "Unknown Prompt")
except:
prompt = "System: Prompt Extraction Failed"

full_thoughts = ""
full_response = ""
record_id = None

async with httpx.AsyncClient(timeout=None) as client:
    try:
        async with client.stream("POST", LLAMA_URL, json=payload) as response:
            async for line in response.aiter_lines():
                if not line: continue
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        raw_data = line[6:].strip()
                        if not raw_data: continue
                        data = json.loads(raw_data)
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        if "reasoning_content" in delta: 
                            full_thoughts += delta["reasoning_content"]
                        if "content" in delta: 
                            full_response += delta["content"]
                        if not record_id and (full_thoughts or full_response):
                            try:
                                create_res = await client.post(PB_URL, json={"prompt": prompt, "thoughts": full_thoughts, "response": full_response, "user": persona_id})
                                if create_res.status_code in [200, 201]: 
                                    record_id = create_res.json().get("id")
                            except: pass
                        yield line + "\n\n"
                    except: continue
                elif line == "data: [DONE]":
                    if record_id:
                        try: 
                            await client.patch(f"{PB_URL}/{record_id}", json={"thoughts": full_thoughts, "response": full_response})
                        except: pass
                    yield line + "\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"


@app.post("/query")
async def query_endpoint(request: Request):
try:
raw_payload = await request.json()
except:
raw_payload = {}
sanitized_payload = ensure_payload_integrity(raw_payload)
return StreamingResponse(stream_and_capture(sanitized_payload), media_type="text/event-stream")

if name == "main":
import uvicorn
uvicorn.run(app, host="127.0.0.1", port=3000)
