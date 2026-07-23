import sys
sys.path.append('/Users/ctavolazzi/Code/_experiments/SimpleAgentOS')
import daily_note

top3_content = """- [x] Query yesterday's `section_operations` PB — verify dogfood persistence
- [x] Build `tools/vault-backup.sh` (Phase 0 from devlog)
- [x] POSTFLIGHT stale empirica transactions (Declared Ghosted)
"""
daily_note.write_section("tomorrows_top_3", top3_content, actor="system")
print("\033[1;32m[ FogSift ]\033[0m Task ledger updated. Stale transactions marked as ghosted.")
