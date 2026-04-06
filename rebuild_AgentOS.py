import os
import shutil
import subprocess
import urllib.request
import zipfile

# --- CONFIGURATION ---
ENGINE_DIR = "core_engine"
PB_VERSION = "0.22.8"
PB_URL = f"https://github.com/pocketbase/pocketbase/releases/download/v{PB_VERSION}/pocketbase_{PB_VERSION}_darwin_amd64.zip"
MODEL_PATH = "/Users/ctavolazzi/google_gemma-4-E4B-it-Q4_K_M.gguf"

def rebuild():
    print(f"📡 Deploying Ironclad OS Architecture...")
    # Kill existing processes
    for port in [8080, 8090, 3000, 5173]:
        subprocess.run(f"kill -9 $(lsof -t -i:{port}) 2>/dev/null || true", shell=True)
    
    if os.path.exists(ENGINE_DIR):
        shutil.rmtree(ENGINE_DIR)
    
    os.makedirs(ENGINE_DIR)
    root = os.path.abspath(ENGINE_DIR)
    frontend = os.path.join(root, "frontend")

    # 1. POCKETBASE
    urllib.request.urlretrieve(PB_URL, os.path.join(root, "pb.zip"))
    with zipfile.ZipFile(os.path.join(root, "pb.zip"), 'r') as z:
        z.extract("pocketbase", path=root)
    os.remove(os.path.join(root, "pb.zip"))
    os.chmod(os.path.join(root, "pocketbase"), 0o755)

    # 2. MIGRATIONS
    os.makedirs(os.path.join(root, "pb_migrations"), exist_ok=True)
    with open(os.path.join(root, "pb_migrations/1712250000_init.js"), "w") as f:
        f.write('''migrate((db) => {
    const dao = new Dao(db);
    const collection = new Collection({
        "name": "transmissions",
        "type": "base",
        "schema": [
            { "name": "prompt", "type": "text" },
            { "name": "thoughts", "type": "text" },
            { "name": "response", "type": "text" }
        ],
        "listRule": "", "viewRule": "", "createRule": "", "updateRule": ""
    });
    return dao.saveCollection(collection);
})''')

    # 3. THE NERVE CENTER (FastAPI - nerve_center.py)
    # Sequential logic to prevent ERR_INCOMPLETE_CHUNKED_ENCODING
    with open(os.path.join(root, "nerve_center.py"), "w") as f:
        f.write('''
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

                        yield f"{line}\\n\\n"
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
''')

    # 4. THE HUD (React - App.jsx)
    os.makedirs(os.path.join(frontend, "src"), exist_ok=True)
    with open(os.path.join(frontend, "package.json"), "w") as f:
        f.write('''{"name":"frontend","type":"module","scripts":{"dev":"vite"},"dependencies":{"react":"^18.2.0","react-dom":"^18.2.0","lucide-react":"^0.363.0"},"devDependencies":{"@vitejs/plugin-react":"^4.2.1","vite":"^5.2.0"}}''')
    
    with open(os.path.join(frontend, "index.html"), "w") as f:
        f.write('<!DOCTYPE html><html><body style="margin:0;background:#050505"><div id="root"></div><script type="module" src="/src/main.jsx"></script></body></html>')

    with open(os.path.join(frontend, "src/main.jsx"), "w") as f:
        f.write('import React from "react";import ReactDOM from "react-dom/client";import App from "./App.jsx";ReactDOM.createRoot(document.getElementById("root")).render(<App />);')

    with open(os.path.join(frontend, "src/App.jsx"), "w") as f:
        f.write('''import React, { useState } from 'react';
import { Activity, Terminal } from 'lucide-react';

export default function App() {
    const [prompt, setPrompt] = useState('');
    const [thoughts, setThoughts] = useState('');
    const [response, setResponse] = useState('');
    const [loading, setLoading] = useState(false);

    const run = async () => {
        if (!prompt) return;
        setLoading(true); setThoughts(''); setResponse('');
        
        try {
            const res = await fetch("http://localhost:3000/query", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ messages: [{role: "user", content: prompt}], stream: true })
            });

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\\n');
                buffer = lines.pop();

                for (const line of lines) {
                    if (line.startsWith('data: ') && line !== 'data: [DONE]') {
                        try {
                            const payload = JSON.parse(line.slice(6));
                            const delta = payload.choices[0].delta;
                            if (delta.reasoning_content) setThoughts(t => t + delta.reasoning_content);
                            if (delta.content) setResponse(r => r + delta.content);
                        } catch (e) {}
                    }
                }
            }
        } catch (err) {
            console.error("NETWORK_ERROR:", err);
        } finally {
            setLoading(false);
        }
    };

    return (
        <div style={{ background: '#050505', color: '#00ff41', minHeight: '100vh', padding: '30px', fontFamily: 'monospace' }}>
            <header style={{ display: 'flex', gap: '20px', marginBottom: '30px', borderBottom: '1px solid #004411', paddingBottom: '15px' }}>
                <Activity size={20} /> <b>SIMPLE_AGENT_OS // FAST-API_RELAY</b>
            </header>
            <div style={{ display: 'grid', gridTemplateColumns: '400px 1fr', gap: '30px' }}>
                <aside>
                    <textarea 
                        value={prompt} 
                        onChange={e => setPrompt(e.target.value)} 
                        style={{ width: '100%', height: '300px', background: '#000', color: '#00ff41', border: '1px solid #004411', padding: '15px', outline: 'none' }} 
                        placeholder="System Directive..."
                    />
                    <button 
                        onClick={run} 
                        disabled={loading}
                        style={{ width: '100%', padding: '20px', background: loading ? '#222' : '#00ff41', color: '#000', border: 'none', fontWeight: 'bold', marginTop: '15px', cursor: 'pointer' }}
                    >
                        {loading ? 'BUSY...' : 'EXECUTE'}
                    </button>
                </aside>
                <main style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
                    <div style={{ flex: 1, border: '1px solid #111', padding: '20px', overflowY: 'auto', background: '#000' }}>
                        <div style={{ color: '#006622', marginBottom: '10px', fontSize: '0.8rem' }}>&gt; THOUGHT_TRACE</div>
                        <div style={{ whiteSpace: 'pre-wrap' }}>{thoughts}</div>
                    </div>
                    <div style={{ flex: 2, border: '1px solid #111', padding: '20px', overflowY: 'auto', background: '#000' }}>
                        <div style={{ color: '#006622', marginBottom: '10px', fontSize: '0.8rem' }}>&gt; OUTPUT_STREAM</div>
                        <div style={{ color: '#eee', whiteSpace: 'pre-wrap' }}>{response}</div>
                    </div>
                </main>
            </div>
        </div>
    );
}''')

    # 5. MAKEFILE
    with open(os.path.join(root, "Makefile"), "w") as f:
        f.write(f'''
setup:
\tcd frontend && npm install
\tpip install fastapi uvicorn httpx

dev:
\t(sleep 3 && open http://localhost:5173) &
\tnpx concurrently \\
\t\t"~/Code/llama.cpp/build/bin/llama-server -m {MODEL_PATH} --port 8080" \\
\t\t"./pocketbase serve" \\
\t\t"python3 nerve_center.py" \\
\t\t"cd frontend && npm run dev" \\
\t\t--names "BRAIN,DB,RELAY,HUD" \\
\t\t--prefix_colors "magenta,yellow,green,cyan"
''')

    print("🚀 Installing dependencies...")
    subprocess.run("npm install", shell=True, cwd=frontend)
    print(f"\n✅ IRONCLAD ARCHITECTURE READY AT ./{ENGINE_DIR}")

if __name__ == "__main__":
    rebuild()