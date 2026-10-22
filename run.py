"""Railway entry: run the engine exactly as before, then the daily P&L step (which only acts at local 00:xx / 06:xx)."""
import subprocess, sys
rc = subprocess.call([sys.executable, 'kill_engine_google_auto.py'] + sys.argv[1:])
try: subprocess.call([sys.executable, 'pnl_daily.py'], timeout=600)
except Exception as ex: print('[pnl] could not run:', ex)
sys.exit(rc)
