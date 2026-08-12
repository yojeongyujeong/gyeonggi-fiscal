"""전년 대비 당초예산 증가율 (17개 광역시도 본청, 2013~2024).

증가율 = (당해 당초 - 전년 당초) / 전년 당초 * 100

주의: budget_revision.csv의 '증감률'(당초 -> 최종, 같은 해 안의 추경)과는
      다른 지표다. 이 파일은 연도 간 당초예산의 증가율을 본다.

신규 수집 없음. data/raw/ACEXBG_*.xml 만 읽는다.
본청 판별은 laf_cd == wa_laf_cd (ACEXBG는 wa_laf_cd를 출력한다).

산출물: data/processed/budget_growth.csv
"""

import csv
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
CSV_PATH = PROJECT_ROOT / "data" / "processed" / "budget_growth.csv"


def load_hq_initial() -> dict[tuple[int, str], int]:
    """(회계연도, 지역명) -> 본청 당초 세출총계."""
    out: dict[tuple[int, str], int] = {}
    for path in sorted(RAW_DIR.glob("ACEXBG_*.xml")):
        root = ET.fromstring(path.read_text(encoding="utf-8"))
        for row in root.findall("row"):
            if row.findtext("laf_cd") != row.findtext("wa_laf_cd"):
                continue  # 본청만
            amt = row.findtext("ane_tott_amt")
            if amt is None:
                continue
            out[(int(row.findtext("fyr")), row.findtext("wa_laf_hg_nm"))] = int(amt)
    return out


def main() -> int:
    hq = load_hq_initial()
    years = sorted({y for y, _ in hq})
    regions = sorted({r for _, r in hq})
    print(f"본청 당초예산 관측치: {len(hq)}건 | 연도 {years[0]}~{years[-1]} | 지역 {len(regions)}개")

    rows = []
    missing_base = []
    for year in years:
        if year - 1 not in years:
            continue
        for region in regions:
            cur = hq.get((year, region))
            prev = hq.get((year - 1, region))
            if cur is None:
                continue
            if prev is None:
                missing_base.append((year, region))
                continue
            rows.append(
                {
                    "회계연도": year,
                    "지역명": region,
                    "전년당초": prev,
                    "당해당초": cur,
                    "증가율": round((cur - prev) / prev * 100, 4),
                }
            )

    rows.sort(key=lambda r: (r["회계연도"], r["지역명"]))

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(
            fh, fieldnames=["회계연도", "지역명", "전년당초", "당해당초", "증가율"]
        )
        w.writeheader()
        w.writerows(rows)

    print(f"[CSV] {CSV_PATH} — {len(rows)}행")
    if missing_base:
        print(f"[전년값 없어 제외] {len(missing_base)}건: {missing_base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
