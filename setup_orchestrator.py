import sqlite3
import sys
import urllib.request
import urllib.error
import json
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Initialize FastAPI App
app = FastAPI(
    title="FogSift Setup Orchestrator",
    description="Local control panel for automated setup, telemetry verification, and epistemic cleanup.",
    version="0.1.0"
)

# Hardcoded Epistemic Paths
EMPIRICA_DB_PATH = "/Users/ctavolazzi/Code/.empirica/sessions/sessions.db"
POCKETBASE_HEALTH_URL = "http://127.0.0.1:8090/api/health"
HARNESS_DIR = "/Users/ctavolazzi/Code/_experiments/SimpleAgentOS"

# Inject harness directory safely for local imports
if HARNESS_DIR not in sys.path:
    sys.path.append(HARNESS_DIR)


class ReconcileRequest(BaseModel):
    content: str


@app.get("/")
def read_root():
    return {"status": "online", "directive": "Maintain Watch", "system": "FogSift Orchestrator"}


@app.get("/health/pocketbase")
def check_pocketbase():
    """Safely pings PocketBase without relying on terminal curl commands."""
    try:
        req = urllib.request.Request(POCKETBASE_HEALTH_URL, method="GET")
        with urllib.request.urlopen(req, timeout=2) as response:
            data = json.loads(response.read().decode())
            return {"status": "online", "pocketbase_response": data}
    except urllib.error.URLError as e:
        return {"status": "offline", "error": f"Connection refused or server down: {e.reason}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/diagnostics/ghosts")
def check_ghost_transactions():
    """Extracts stale transaction UUIDs across the distributed schema non-destructively."""
    db_path = Path(EMPIRICA_DB_PATH)
    if not db_path.exists():
        return {"status": "error", "message": "sessions.db not found at expected path."}

    query = """
    SELECT transaction_id FROM goals WHERE transaction_id LIKE 'fc5cb098%' OR transaction_id LIKE '43ca9c6a%'
    UNION
    SELECT transaction_id FROM project_findings WHERE transaction_id LIKE 'fc5cb098%' OR transaction_id LIKE '43ca9c6a%'
    UNION
    SELECT transaction_id FROM project_unknowns WHERE transaction_id LIKE 'fc5cb098%' OR transaction_id LIKE '43ca9c6a%'
    UNION
    SELECT transaction_id FROM project_dead_ends WHERE transaction_id LIKE 'fc5cb098%' OR transaction_id LIKE '43ca9c6a%'
    UNION
    SELECT transaction_id FROM mistakes_made WHERE transaction_id LIKE 'fc5cb098%' OR transaction_id LIKE '43ca9c6a%'
    UNION
    SELECT transaction_id FROM reflexes WHERE transaction_id LIKE 'fc5cb098%' OR transaction_id LIKE '43ca9c6a%'
    UNION
    SELECT transaction_id FROM assumptions WHERE transaction_id LIKE 'fc5cb098%' OR transaction_id LIKE '43ca9c6a%'
    UNION
    SELECT transaction_id FROM decisions WHERE transaction_id LIKE 'fc5cb098%' OR transaction_id LIKE '43ca9c6a%';
    """
    
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            results = [row[0] for row in cursor.fetchall()]
            
            return {
                "status": "success", 
                "ghosts_found": len(results), 
                "uuids": results,
                "note": "If empty, these transactions are confirmed as unrecoverable ghosts."
            }
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.post("/reconcile/ledger")
def reconcile_daily_note(req: ReconcileRequest):
    """Uses the daily_note.py API to safely check off tasks."""
    try:
        import daily_note
        daily_note.write_section("tomorrows_top_3", req.content, actor="system")
        return {"status": "success", "message": "Task ledger successfully reconciled via API."}
    except ImportError:
        raise HTTPException(status_code=500, detail="Could not import daily_note.py. Check HARNESS_DIR path.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    print("\033[1;34m[ SYSTEM ]\033[0m Starting FogSift Setup Orchestrator on port 8000...")
    uvicorn.run(app, host="127.0.0.1", port=8000)
