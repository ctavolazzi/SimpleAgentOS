#!/bin/bash
PORTS=(3000 8000 8080 8090)
WORKSPACE="/Users/ctavolazzi/Code/_experiments/SimpleAgentOS"
echo -e "\033[1;31m[ TEARDOWN ]\033[0m Severing connections..."
for PORT in "${PORTS[@]}"; do
    PID=$(lsof -ti :$PORT)
    [ ! -z "$PID" ] && [ "$PID" -gt 100 ] && kill -9 $PID 2>/dev/null && sleep 0.2
done
SERVICES=("collect_agent_state.py" "model_server.py" "setup_orchestrator.py" "nerve_center.py" "test_transmission.py" "pocketbase")
for S in "${SERVICES[@]}"; do pkill -9 -f "$S" 2>/dev/null; done
find "$WORKSPACE" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
[ -f "$WORKSPACE/core_engine/pb_data/data.db-journal" ] && rm "$WORKSPACE/core_engine/pb_data/data.db-journal"
echo -e "\033[1;32m[ COMPLETE ]\033[0m Ready for spawn_beast.py."
