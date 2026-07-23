import json
import httpx
import sqlite3
import os
import asyncio

async def main():
    print("\033[1;34m[ SYSTEM ]\033[0m Initiating End-to-End Transmission Test...")
    workspace = '/Users/ctavolazzi/Code/_experiments/SimpleAgentOS'
    pb_db = os.path.join(workspace, 'core_engine/pb_data/data.db')
    nerve_center_url = "http://127.0.0.1:3000/query"

    payload = {
        "persona_id": "persona_fogsift",
        "messages": [{"role": "user", "content": "FogSift, confirm system status."}]
    }

    print(f"[ SENDING ] Transmission as: {payload['persona_id']}")
    
    full_response = ""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("POST", nerve_center_url, json=payload) as response:
                if response.status_code != 200:
                    print(f"\033[1;31m[ FAIL ]\033[0m Nerve Center returned {response.status_code}")
                    return

                async for line in response.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            chunk = json.loads(line[6:])
                            content = chunk["choices"][0]["delta"].get("content", "")
                            full_response += content
                            # Print streaming response to terminal
                            print(content, end="", flush=True)
                        except:
                            pass
        print("\n[ STREAM COMPLETE ]")

        # 2. Database Verification
        print("\033[1;34m[ SYSTEM ]\033[0m Verifying Database Anchorage...")
        conn = sqlite3.connect(pb_db)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Check if the last transmission has the correct user link
        cursor.execute("""
            SELECT id, prompt, user, created 
            FROM transmissions 
            ORDER BY created DESC LIMIT 1
        """)
        record = cursor.fetchone()
        
        if record and record['user'] == 'persona_fogsift':
            print(f"\033[1;32m[ SUCCESS ]\033[0m Transmission anchored to: {record['user']}")
            print(f"[ RECORD ID ] {record['id']}")
        else:
            print("\033[1;31m[ FAIL ]\033[0m Anchorage failed or record not found.")
            if record:
                print(f"[ DEBUG ] Found user: {record['user']} (Expected: persona_fogsift)")
        
        conn.close()

    except Exception as e:
        print(f"\033[1;31m[ ERROR ]\033[0m Connection failed: {e}")
        print("Note: Ensure 'core_engine/nerve_center.py' is running on port 3000.")

if __name__ == "__main__":
    asyncio.run(main())
