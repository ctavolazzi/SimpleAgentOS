import socket, time, os, sys

PORTS = {"Nerve Center": 3000, "Orchestrator": 8000, "Model Server": 8080, "PocketBase": 8090, "Tool Registry": 5000, "Memory Store": 6379}

def check_port(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.1)
        return s.connect_ex(('127.0.0.1', port)) == 0 or s.connect_ex(('0.0.0.0', port)) == 0

def main():
    try:
        while True:
            all_clear = True
            status = []
            for name, port in PORTS.items():
                if check_port(port):
                    status.append(f"{name:15} (Port {port}): \033[1;32m✅ ONLINE\033[0m")
                else:
                    status.append(f"{name:15} (Port {port}): \033[1;31m⳸ WAITING\033[0m")
                    all_clear = False
            os.system('clear')
            print("\033[1;34m[ HEALTH MONITOR ]\033[0m Scanning AgentOS Core...\n" + "—"*40)
            print("\n".join(status))
            print("—"*40)
            if all_clear:
                print("\033[1;32m[ BEAST IS LIVE ]\033[0m All systems operational.\n\n\033[90mMonitoring active... (Ctrl+C to close)\033[0m")
                time.sleep(2)
            else:
                print("\033[1;33m[ BOOT SEQUENCE ]\033[0m Status gating: Waiting for subsystems...")
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n\033[1;31m[ SUSPENDED ]\033[0m Monitor offline.")
        sys.exit(0)

if __name__ == "__main__": main()