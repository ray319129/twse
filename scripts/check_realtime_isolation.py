"""鐵則一的守門員:即時資料絕不進選股層。

使用者 2026-07-19 明確要求。理由:FinMind Sponsor 是按月訂閱,一旦 `scoring` /
`indicators` / `backtest` 依賴只有 Sponsor 拿得到的欄位(均價/量比/委買賣),
訂閱到期那天選股會直接壞掉 —— 而且是**安靜地**壞掉:不會 raise,只會靜靜地
少一個因子、分數整體偏移,等你發現已經照著錯的名單買了好幾週。

回測層更嚴重:用即時欄位算出來的回測結果,在沒有訂閱的機器上**永遠重現不了**。

所以這條線用程式碼守,不是用註解拜託自己記得。CI 與 daily workflow 都會跑。

    python -m scripts.check_realtime_isolation     # 違規回 exit 1
"""
from __future__ import annotations
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 這些模組是「選股 / 研究」層,結果必須在沒有 Sponsor 訂閱時也完全一致
PROTECTED = ["scoring.py", "indicators.py", "backtest.py", "screener.py", "industry.py"]

# 這些模組承載即時資料,上面那些不准碰
REALTIME = {"quotes", "snapshot_archive"}


def _violations(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except Exception as e:
        return [f"{path.name}: 無法解析({e})"]
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "").lstrip(".")
            if mod.split(".")[0] in REALTIME:
                bad.append(f"{path.name}:{node.lineno} from {node.module} import …")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[-1] in REALTIME:
                    bad.append(f"{path.name}:{node.lineno} import {a.name}")
    return bad


def main() -> int:
    bad: list[str] = []
    for name in PROTECTED:
        p = ROOT / name
        if p.exists():
            bad += _violations(p)
    if bad:
        print("✗ 鐵則一違規:選股/回測層 import 了即時報價模組")
        for b in bad:
            print("   " + b)
        print("\n即時資料只能餵『監控 / 執行 / 顯示』層。訂閱到期時選股必須完全不受影響。")
        return 1
    print(f"✓ 鐵則一 OK:{len(PROTECTED)} 個選股/回測模組都沒有依賴即時報價")
    return 0


if __name__ == "__main__":
    sys.exit(main())
