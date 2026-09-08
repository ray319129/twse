"""跑完 tests/ 下所有測試。本專案沒有 pytest 依賴,所以自己走一遍。

    python tests/run_all.py
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
files = sorted(p for p in HERE.glob("test_*.py"))
fails = []
for f in files:
    print(f"\n=== {f.name} ===")
    r = subprocess.run([sys.executable, str(f)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    out = "\n".join(l for l in (r.stdout or "").splitlines()
                    if not l.startswith("2026-") and "INFO" not in l)
    print(out.strip())
    if r.returncode:
        fails.append(f.name)
        if r.stderr:
            print(r.stderr.strip()[:800])
print("\n" + "=" * 50)
print(f"{len(files)} 個測試檔,{'全部通過' if not fails else '失敗:' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
