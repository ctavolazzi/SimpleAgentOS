import React, { useState } from 'react';
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
                const lines = buffer.split('\n');
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
}