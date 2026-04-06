
import json
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

PB_URL = "http://127.0.0.1:8090/api/collections/transmissions/records"
LLAMA_URL = "http://127.0.0.1:8080/v1/chat/completions"

async def stream_and_capture(payload):
    prompt = payload["messages"][-1]["content"]
    full_thoughts = ""
    full_response = ""
    record_id = None

    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream("POST", LLAMA_URL, json=payload) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        data = json.loads(line[6:])
                        delta = data["choices"][0]["delta"]

                        if "reasoning_content" in delta:
                            full_thoughts += delta["reasoning_content"]
                        if "content" in delta:
                            full_response += delta["content"]

                        # CREATE on first bit of data
                        if not record_id and (full_thoughts or full_response):
                            create_res = await client.post(PB_URL, json={
                                "prompt": prompt, "thoughts": full_thoughts, "response": full_response
                            })
                            record_id = create_res.json().get("id")
                        
                        # UPDATE every ~20 characters to keep DB in sync without killing performance
                        elif record_id and len(full_response + full_thoughts) % 20 == 0:
                            await client.patch(f"{PB_URL}/{record_id}", json={
                                "thoughts": full_thoughts, "response": full_response
                            })

                        yield f"{line}\n\n"
        except Exception as e:
            print(f"STREAME_ERROR: {e}")
        finally:
            # FINAL GUARANTEE: Save everything on stop/fail
            if record_id:
                async with httpx.AsyncClient() as final_client:
                    await final_client.patch(f"{PB_URL}/{record_id}", json={
                        "thoughts": full_thoughts, "response": full_response
                    })

@app.post("/query")
async def query(request: Request):
    payload = await request.json()
    return StreamingResponse(stream_and_capture(payload), media_type="text/event-stream")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3000)
