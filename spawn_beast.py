import os
import subprocess
import sys
import time

WORKSPACE = "/Users/ctavolazzi/Code/_experiments/SimpleAgentOS"
CORE_ENGINE = os.path.join(WORKSPACE, "core_engine")
TEARDOWN_SCRIPT = os.path.join(WORKSPACE, "kill_beast.sh")

def run_applescript(script):
    try:
        subprocess.run(['osascript', '-e', script], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error: {e}")

def ensure_clean_start():
    if os.path.exists(TEARDOWN_SCRIPT):
        subprocess.run(['bash', TEARDOWN_SCRIPT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

def spawn():
    if not os.path.exists(WORKSPACE):
        sys.exit(1)
    ensure_clean_start()
    script = f'''
    tell application "iTerm"
        set newWindow to (create window with default profile)
        tell current session of newWindow
            write text "cd {WORKSPACE} && python3 collect_agent_state.py"
            set pane2 to (split vertically with default profile)
            tell pane2
                write text "cd {WORKSPACE} && python3 model_server.py"
            end tell
            set pane3 to (split horizontally with default profile)
            tell pane3
                write text "cd {WORKSPACE} && sleep 4 && python3 test_transmission.py"
            end tell
            tell current session of newWindow
                set pane4 to (split horizontally with default profile)
                tell pane4
                    write text "cd {WORKSPACE} && python3 core_engine/nerve_center.py & {CORE_ENGINE}/pocketbase serve & python3 setup_orchestrator.py"
                end tell
            end tell
        end tell
    end tell
    '''
    run_applescript(script)

if __name__ == "__main__":
    spawn()
