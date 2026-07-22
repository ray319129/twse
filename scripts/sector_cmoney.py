"""CMoney 產業類股分類 → docs/sector_map.json(2026-07-22)。

取代原本的 FinMind 產業鏈(industry_chain.py)當作熱力圖與市場氛圍的分組依據。
使用者自己從 CMoney 抓的 `產業類股總覽`(data/cmoney_categories.json):
**87 個類股、6 大分類、每檔恰好屬於一個類股**(不像 FinMind 產業鏈一檔跨多條)。

## 為什麼換掉 FinMind 產業鏈

FinMind 是「一檔對多條鏈」(鴻海同時在電腦/通信/連接器/電動車),市場氛圍算各族群
時同一檔會被重複計入好幾組。CMoney 是散戶實際在看的「類股」,一檔一類、乾淨,
而且家數/歸屬更貼近盤面在講的族群。

## 使用者指定:只用「類股」一層,不要細產業(2026-07-22)

CMoney 有兩層(6 大分類 + 87 類股),但使用者選擇**只用 87 類股平鋪**、不要大分類
roll-up 也不要更細的細產業。所以 sec 與 sub 都設成類股名稱本身:
  primary[sid] = [類股, 類股]
  chain[sid]   = [[類股, 類股]]      # 單一元素,因為一檔只屬一類
前端偵測到 sub==sec 時會把 treemap 收合成單層(類股 → 個股),
市場氛圍也只剩 87 類股一層、不顯示「細產業」切換(見 docs/index.html)。

## 為什麼沿用 sector_map.json 的既有格式(chain/primary)

前端 hmSecsOf / hmSecOf / moodAgg / _buildHmTree 全部吃這個格式。維持格式 =
後端換資料源、前端幾乎零改動(只需收合單層與拿掉細產業鈕)。

## CI:資料是 commit 進 repo 的靜態檔

CMoney 沒有免費 API,這份是使用者手動抓的快照。分類變動很慢(只有新上市/改隸屬),
所以直接 commit `data/cmoney_categories.json`,每天批次用它重建 sector_map.json。
使用者日後重抓覆蓋這個檔即可,不需要改程式。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .config import DATA_DIR
from .utils import log

SOURCE = DATA_DIR / "cmoney_categories.json"


def load_categories() -> dict:
    """回傳 {stock_id: (類股, 大分類)}。找不到來源檔就回空 dict(呼叫端沿用舊 sector_map)。"""
    if not SOURCE.exists():
        log.warning(f"CMoney 分類來源不存在:{SOURCE};沿用既有 sector_map.json。")
        return {}
    try:
        raw = json.loads(SOURCE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"CMoney 分類讀取失敗:{e};沿用既有 sector_map.json。")
        return {}
    out: dict[str, tuple[str, str]] = {}
    for c in raw.get("categories", []):
        cat = str(c.get("category_name") or "").strip()
        parent = str(c.get("parent_category") or "").strip()
        if not cat:
            continue
        for s in c.get("stocks", []):
            sid = str(s.get("stock_id") or "").strip()
            if sid and sid not in out:      # 一檔一類;萬一有重複,取先出現的
                out[sid] = (cat, parent)
    return out


def build_sector_map(out_path: Path | str) -> dict:
    """讀 CMoney 分類 → 寫 docs/sector_map.json(格式與 FinMind 版相容)。

    使用者選擇「只用類股一層」→ sec 與 sub 都是類股名稱。
    來源檔缺失或空 → **不覆寫**既有 sector_map.json(回傳 None),避免把好檔洗成空的。
    """
    cats = load_categories()
    if not cats:
        return None                          # 呼叫端會 log 並沿用舊檔

    # ⚠️ **同名類股要用大分類消歧義。** CMoney 有一個「其他」同時掛在 4 個大分類下
    # (傳產 116 檔、電子中游 19、電子下游 12、軟體 23)。若只用名稱分組,這 170 檔
    # 毫不相干的股票會被併成一個巨大的「其他」桶 —— 熱力圖會被它灌爆、市場氛圍也失真。
    # 對「跨大分類同名」的類股一律加上大分類後綴,還原成使用者要的 87 個獨立類股。
    from collections import defaultdict
    parents_of = defaultdict(set)
    for _cat, _parent in cats.values():
        parents_of[_cat].add(_parent)
    collide = {c for c, ps in parents_of.items() if len(ps) > 1}

    def _label(cat: str, parent: str) -> str:
        return f"{cat}·{parent}" if cat in collide and parent else cat

    chain: dict[str, list] = {}
    primary: dict[str, list] = {}
    for sid, (cat, parent) in cats.items():
        name = _label(cat, parent)
        # sec == sub == 類股:前端據此收合成單層(見檔頭說明)
        primary[sid] = [name, name]
        chain[sid] = [[name, name]]

    n_cats = len({primary[s][0] for s in primary})
    n_parents = len({p for _, p in cats.values() if p})
    payload = {
        "_source": "CMoney 產業類股總覽(data/cmoney_categories.json)",
        "_built": date.today().isoformat(),
        "_stats": {"stocks": len(cats), "industries": n_cats,
                   "parents": n_parents, "single_level": True},
        "chain": chain,
        "primary": primary,
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    log.info(f"sector_map.json 已由 CMoney 分類重建:{len(cats)} 檔 / {n_cats} 類股 / {n_parents} 大分類")
    return payload


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else str(DATA_DIR.parent / "docs" / "sector_map.json")
    r = build_sector_map(out)
    print("OK" if r else "來源缺失,未覆寫")
