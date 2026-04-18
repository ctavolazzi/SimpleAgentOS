import os
import sys

# Add harness to path
sys.path.append('/Users/ctavolazzi/Code/_experiments/SimpleAgentOS')
import daily_note

def check_off_tasks():
    # We mark the vault-backup.sh task as complete [x]
    top3_content = """- [ ] Query yesterday's `section_operations` PB — verify dogfood persistence
- [x] Build `tools/vault-backup.sh` (Phase 0 from devlog)
- [ ] POSTFLIGHT stale empirica transactions `fc5cb098…`, `43ca9c6a…`
"""
    # Use the API to overwrite the section
    daily_note.write_section("tomorrows_top_3", top3_content, actor="system")
    print("\033[1;32m[ FogSift ]\033[0m Updated Top 3 list (marked vault-backup.sh as complete).")

if __name__ == "__main__":
    check_off_tasks()
