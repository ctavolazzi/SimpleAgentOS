import json
import urllib.request
import time

# --- CONFIGURATION ---
# Points to the PocketBase instance managed by core_engine
DB_URL = "http://localhost:8090/api/collections/transmissions/records"

# Diagnostic entries to test the Monitoring UI's visual ranges
SEED_DATA = [
    {
        "prompt": "DIAGNOSTIC: Performance Baseline",
        "thoughts": "Calibrating sensors... Testing token throughput... Buffer cleared.",
        "response": "Baseline established. Neural engine reporting 100% integrity.",
        "ttft": 0.28,
        "tps": 48.50,
        "model_id": "gemma-4-it-base"
    },
    {
        "prompt": "STRESS_TEST: Deep Recursive Reasoning",
        "thoughts": "Tracing logic branches... depth 15... depth 45... searching for logical exit. Reasoning engine under heavy load.",
        "response": "Stress test complete. System maintained stability under high-context recursion.",
        "ttft": 2.15,
        "tps": 18.22,
        "model_id": "gemma-4-it-logic"
    },
    {
        "prompt": "LOAD_TEST: Maximum Token Stream",
        "thoughts": "Bypassing reasoning filters for raw throughput optimization.",
        "response": "Stream test active. High-frequency token generation confirmed. Dashboard should reflect peak TPS.",
        "ttft": 0.15,
        "tps": 62.40,
        "model_id": "gemma-4-it-speed"
    },
    {
        "prompt": "MEMORY_TEST: Historical Recall",
        "thoughts": "Cross-referencing seeded transmissions... parity check initiated.",
        "response": "Database write/read cycles verified. Persistence layer is persistent.",
        "ttft": 0.55,
        "tps": 35.10,
        "model_id": "gemma-4-it-base"
    }
]

def seed_system():
    print(f"📡 Initializing System Seed Sequence...")
    print(f"🔗 Target: {DB_URL}")
    
    success_count = 0
    for i, item in enumerate(SEED_DATA):
        try:
            # Prepare the POST request
            data = json.dumps(item).encode('utf-8')
            req = urllib.request.Request(
                DB_URL, 
                data=data,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            
            # Execute
            with urllib.request.urlopen(req) as res:
                if res.status == 200:
                    print(f"  [{i+1}/{len(SEED_DATA)}] OK: {item['prompt'][:25]}...")
                    success_count += 1
            
            # Tiny sleep to ensure unique 'created' timestamps in the DB
            time.sleep(0.1)
            
        except Exception as e:
            print(f"  [{i+1}/{len(SEED_DATA)}] FAILED: {e}")
            print("  ⚠️ Ensure 'make dev' is running and Port 8090 is open!")
            return

    if success_count == len(SEED_DATA):
        print(f"\n✨ SUCCESS: {success_count} transmissions injected into core_engine.")
        print(f"📊 View the new data at http://localhost:5173")

if __name__ == "__main__":
    seed_system()