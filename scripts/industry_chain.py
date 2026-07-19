"""FinMind 產業鏈分類(TaiwanStockIndustryChain)→ docs/sector_map.json。

取代原本手工維護的 46 條 industry_map + 306 筆 override。差別:
  * 舊版是「一檔股票 = 一個產業」,靠 TWSE 的粗分類(電子零組件業/其他電子業…)再人工修。
  * FinMind 產業鏈是「一檔股票 = 多條產業鏈」(47 個產業 / 484 個細產業),
    鴻海同時在 電腦及週邊設備(伺服器/主機板/機殼…)、通信網路、連接器、電動車輛。
    這才是台股實際講的「族群」——市場氛圍要的就是這種歸屬。

因此輸出兩份:
  chain   {id: [[產業, 細產業], ...]}  市場氛圍用,一檔可計入多個族群
  primary {id: [產業, 細產業]}         熱力圖 treemap 用,一檔只能放一格

primary 的挑法:先取該檔涵蓋細產業最多的那個產業(鴻海 → 電腦及週邊設備,9 條;
同分時取全市場較大的那個產業),再於該產業內取家數最多、名字不是「其他…」的細產業
(鴻海 → 筆記型電腦)。排掉「其他」是因為 FinMind 每個產業都有一個垃圾桶分類,
不排的話 treemap 會有一大票股票擠在「其他電腦及週邊設備」看不出東西。

primary 只影響 treemap 一格放哪(一檔只能放一格,否則成交額會重複計);
市場氛圍走完整 chain,中華電這種橫跨 智慧電網 + 通信網路 的兩邊都會算到。

沒被 FinMind 收錄的(多為 KY 外國企業,約佔掃描池 2%)不硬塞,
前端會退回用 TWSE 原始產業名,家數不足自然會被市場氛圍的 n<3 門檻濾掉。
"""
from __future__ import annotations
import json
import re
from collections import Counter, defaultdict
from datetime import datetime

from .config import DATA_DIR
from .fetchers import fetch_finmind
from .utils import log

DOCS_DIR = DATA_DIR.parent / "docs"

# 細產業名稱常帶一長串舉例,如「網路設備(如數據機、網路卡、閘道器、路由器、網路電話)」。
# 前端是窄欄位,括號整段砍掉才讀得下去。
_PAREN = re.compile(r"[(（].*?[)）]")


def _short(name: str) -> str:
    s = _PAREN.sub("", name or "").strip(" 、,")
    return s or (name or "").strip()


def build_sector_map(out_path=None) -> dict | None:
    """抓 FinMind 產業鏈寫成 docs/sector_map.json。失敗回 None 且不動既有檔案
    (分類壞掉不如維持昨天的,熱力圖/市場氛圍不該因為單次 API 掛掉就整頁空白)。"""
    rows = fetch_finmind("TaiwanStockIndustryChain")
    if not rows:
        log.warning("FinMind 產業鏈無資料,sector_map.json 維持原樣。")
        return None

    pairs: dict[str, list[tuple[str, str]]] = defaultdict(list)
    sub_count: Counter[str] = Counter()
    ind_count: Counter[str] = Counter()
    for r in rows:
        sid = str(r.get("stock_id") or "").strip()
        ind = _short(r.get("industry"))
        sub = _short(r.get("sub_industry"))
        if not sid or not ind or not sub:
            continue
        if (ind, sub) in pairs[sid]:
            continue
        pairs[sid].append((ind, sub))
        sub_count[sub] += 1
        ind_count[ind] += 1

    chain, primary = {}, {}
    for sid, ps in pairs.items():
        chain[sid] = [list(p) for p in ps]
        by_ind: Counter[str] = Counter(i for i, _ in ps)
        top_ind = max(by_ind, key=lambda i: (by_ind[i], ind_count[i]))
        subs = [s for i, s in ps if i == top_ind]
        named = [s for s in subs if not s.startswith("其他")] or subs
        # 取家數的「中位數」那一個,不是最多也不是最少 —— 兩端都會挑錯(2026-07-19 實測 10 檔對照):
        #   最多(max) 6/10:偏向製程類的通用桶,旺宏→IC封裝測試、華邦電→晶圓製造(兩家其實是記憶體廠)
        #   最少(min) 6/10:偏向冷門標籤,聯發科→光儲存控制IC、台達電→LED驅動IC
        #   中位(median) 8/10:華邦電→記憶體IC、台達電→電源管理IC、緯創→筆記型電腦 全對
        primary[sid] = [top_ind,
                        sorted(named, key=lambda s: (sub_count[s], s))[len(named) // 2]]

    data = {
        "_source": "FinMind TaiwanStockIndustryChain",
        "_built": datetime.now().strftime("%Y-%m-%d"),
        "_stats": {
            "stocks": len(chain),
            "industries": len({i for ps in pairs.values() for i, _ in ps}),
            "sub_industries": len(sub_count),
        },
        "chain": chain,
        "primary": primary,
    }
    path = out_path or (DOCS_DIR / "sector_map.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    log.info(
        f"產業鏈分類已更新:{len(chain)} 檔 / {data['_stats']['industries']} 產業 "
        f"/ {data['_stats']['sub_industries']} 細產業"
    )
    return data


if __name__ == "__main__":  # 手動重建:python -m scripts.industry_chain
    build_sector_map()
