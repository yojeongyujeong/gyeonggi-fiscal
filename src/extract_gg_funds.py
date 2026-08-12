"""경기도 재정공시 '기금운영현황' 표 추출 (2021~2024 회계연도).

지방재정365 OpenAPI에는 기금 종류별 데이터가 없어(BGGCD/DJAIH 모두 총계),
경기도청 재정공시 원문에서 직접 뽑는다.

원본: data/raw/gg_disclosure/  (경기도청 지방재정공시 게시판, 공공누리 제3유형)

**회계연도와 공시연도가 다르다.** 2025년 공시 = 2024 회계연도.
파일명은 회계연도 기준으로 저장돼 있다(gg_2024.* = 2024 회계연도).

연도별 형식 차이:
  2021: .hwp(구형 바이너리)만 제공 -> PDF + pdfplumber 표인식
  2022~2024: .hwpx(zip+XML) -> 셀 마크업 직접 파싱
  단위: 2021·2022 = 백만원, 2023·2024 = 억원  ('기금운영현황' 뒤 단위 표기로 판정)
  음수: 2021·2022 = '△', 2023·2024 = '-'

모든 금액은 **백만원**으로 정규화한다(억원 -> x100). 원본 단위는 컬럼에 남긴다.

산출물: data/processed/gyeonggi_fund_detail.csv
"""

import csv
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOC_DIR = PROJECT_ROOT / "data" / "raw" / "gg_disclosure"
CSV_PATH = PROJECT_ROOT / "data" / "processed" / "gyeonggi_fund_detail.csv"

YEARS = (2021, 2022, 2023, 2024)
UNIT_TO_MILLION = {"백만원": 1, "억원": 100}

ln = lambda t: t.rsplit("}", 1)[-1]


def norm_num(s: str) -> int | None:
    """'△148', '-2,116', '1,876' -> int. 빈칸은 None."""
    if s is None:
        return None
    t = s.strip().replace(",", "").replace(" ", "")
    # 금액 칸의 '-' 는 결측이 아니라 0이다(그해 신설된 기금 등).
    # 검산으로 확인: 통합재정안정화기금(통합계정) '-' + 708,747 = 708,747.
    if t == "-":
        return 0
    t = t.replace("△", "-").replace("▲", "-").replace("−", "-").replace("–", "-")
    if t == "":
        return None
    if not re.fullmatch(r"-?\d+", t):
        return None
    return int(t)


def find_unit(text: str) -> str:
    """기금표(일몰기간)에 **가장 가까운** (단위 : XXX) 를 채택한다.

    단위 표기가 표 앞/뒤 어느 쪽에도 올 수 있고 목차에도 같은 제목이 있어,
    제목 기준이 아니라 표 위치 기준으로 찾아야 한다.
    """
    anchors = [m.start() for m in re.finditer("일몰기간", text)]
    if not anchors:
        raise RuntimeError("'일몰기간'을 못 찾음")
    # '(단위 : 천원, 명)' 처럼 다른 항목이 딸린 표기는 이 표의 단위가 아니다.
    # 단위 하나만 들어있고 바로 괄호가 닫히는 표기만 인정한다.
    cands = [
        (m.start(), m.group(1))
        for m in re.finditer(r"단위\s*[:：]?\s*(백만원|억원|천원|원)\s*\)", text)
    ]
    if not cands:
        raise RuntimeError("단위 표기를 못 찾음")
    anchor = anchors[0]
    pos, unit = min(cands, key=lambda c: abs(c[0] - anchor))
    if abs(pos - anchor) > 6000:
        raise RuntimeError(f"단위 표기가 표에서 너무 멂 (거리 {abs(pos-anchor)})")
    return unit


# ---------- HWPX ----------
def rows_from_hwpx(path: Path) -> tuple[list[list[str]], str]:
    z = zipfile.ZipFile(path)
    for name in [n for n in z.namelist() if re.search(r"section\d+\.xml$", n)]:
        raw = z.read(name).decode("utf-8", "replace")
        if "일몰기간" not in raw or "조성액" not in raw:
            continue
        unit = find_unit(re.sub(r"<[^>]+>", "", raw))
        root = ET.fromstring(raw)
        for tbl in (e for e in root.iter() if ln(e.tag) == "tbl"):
            txt = "".join(e.text or "" for e in tbl.iter() if ln(e.tag) == "t")
            if "일몰기간" not in txt or "조성액" not in txt:
                continue
            out = []
            for tr in [e for e in tbl.iter() if ln(e.tag) == "tr"]:
                row = {}
                for tc in [e for e in tr.iter() if ln(e.tag) == "tc"]:
                    addr = [e for e in tc.iter() if ln(e.tag) == "cellAddr"]
                    col = int(addr[0].get("colAddr")) if addr else -1
                    val = "".join(
                        e.text or "" for e in tc.iter() if ln(e.tag) == "t"
                    ).strip()
                    row[col] = val
                if len(row) >= 7:
                    out.append([row.get(i, "") for i in range(7)])
            return out, unit
    raise RuntimeError(f"{path.name}: 기금표를 찾지 못함")


# ---------- PDF ----------
def rows_from_pdf(path: Path) -> tuple[list[list[str]], str]:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        for i, pg in enumerate(pdf.pages):
            text = pg.extract_text() or ""
            if "일몰기간" not in text or "조성액" not in text:
                continue
            unit = find_unit(text)
            out = []
            for tb in pg.extract_tables():
                flat = " ".join(c or "" for r in tb for c in r)
                if "일몰" not in flat or "조성액" not in flat:
                    continue
                for r in tb:
                    cells = [(c or "").replace("\n", "") for c in r]
                    if len(cells) >= 7 and cells[0] and cells[1]:
                        out.append(cells[:7])
            # 다음 페이지로 이어붙이지 않는다. 같은 페이지의 다른 표(지방공공기관/교육재정
            # 등)가 섞여 들어와 검산이 깨지는 것을 확인했다. 표가 한 페이지에 다 들어오는지는
            # 합계행 검산으로 확인한다.
            return out, unit
    raise RuntimeError(f"{path.name}: 기금표를 찾지 못함")


def extract_year(fy: int):
    hwpx = DOC_DIR / f"gg_{fy}.hwpx"
    pdf = DOC_DIR / f"gg_{fy}.pdf"
    if hwpx.exists():
        rows, unit = rows_from_hwpx(hwpx)
        src = hwpx.name
    elif pdf.exists():
        rows, unit = rows_from_pdf(pdf)
        src = pdf.name
    else:
        raise SystemExit(f"[중단] {fy} 회계연도 원본 파일이 없습니다.")

    data, total_row = [], None
    for r in rows:
        name = re.sub(r"\s+", " ", (r[0] or "")).strip()
        if not name or "구 분" in name or "종류별" in name:
            continue
        nums = [norm_num(r[k]) for k in (1, 2, 3, 4, 5)]
        if any(v is None for v in nums):
            continue
        rec = {
            "기금명": name,
            "전년도현재액": nums[0],
            "증감액": nums[1],
            "조성액": nums[2],
            "사용액": nums[3],
            "당해현재액": nums[4],
            "일몰기간": re.sub(r"\s+", "", r[6] or ""),
        }
        if name.replace(" ", "") in ("합계", "합 계", "계"):
            total_row = rec
        else:
            data.append(rec)
    return data, total_row, unit, src


def main() -> int:
    all_rows, report = [], []
    for fy in YEARS:
        data, total, unit, src = extract_year(fy)
        mult = UNIT_TO_MILLION[unit]

        # ---- 검산 (원본 단위 그대로) ----
        bad_ab, bad_cd = [], []
        for d in data:
            if d["전년도현재액"] + d["증감액"] != d["당해현재액"]:
                bad_ab.append(d)
            if d["조성액"] - d["사용액"] != d["증감액"]:
                bad_cd.append(d)
        sums = {
            k: sum(d[k] for d in data)
            for k in ("전년도현재액", "증감액", "조성액", "사용액", "당해현재액")
        }
        bad_total = None
        if total:
            diff = {k: sums[k] - total[k] for k in sums}
            if any(v != 0 for v in diff.values()):
                bad_total = diff

        report.append(
            {
                "fy": fy, "src": src, "unit": unit, "n": len(data),
                "bad_ab": bad_ab, "bad_cd": bad_cd,
                "sums": sums, "total": total, "bad_total": bad_total,
            }
        )

        for d in data:
            all_rows.append(
                {
                    "회계연도": fy,
                    "기금명": d["기금명"],
                    "전년도현재액": d["전년도현재액"] * mult,
                    "증감액": d["증감액"] * mult,
                    "조성액": d["조성액"] * mult,
                    "사용액": d["사용액"] * mult,
                    "당해현재액": d["당해현재액"] * mult,
                    "일몰기간": d["일몰기간"],
                    "원본단위": unit,
                }
            )

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["회계연도", "기금명", "전년도현재액", "증감액", "조성액",
                        "사용액", "당해현재액", "일몰기간", "원본단위"],
        )
        w.writeheader()
        w.writerows(all_rows)

    print(f"[CSV] {CSV_PATH} — {len(all_rows)}행 (금액 단위: 백만원으로 정규화)\n")
    print("=" * 72)
    print("검산 결과")
    print("=" * 72)
    ok = True
    for r in report:
        print(f"\nFY{r['fy']}  출처={r['src']}  원본단위={r['unit']}  기금 {r['n']}개")
        v1 = "OK" if not r["bad_ab"] else f"실패 {len(r['bad_ab'])}건"
        v2 = "OK" if not r["bad_cd"] else f"실패 {len(r['bad_cd'])}건"
        print(f"  [1] 전년도+증감=당해   : {v1}")
        for d in r["bad_ab"]:
            print(f"        - {d['기금명']}: {d['전년도현재액']} + {d['증감액']} "
                  f"= {d['전년도현재액']+d['증감액']} ≠ {d['당해현재액']}")
        print(f"  [2] 조성-사용=증감     : {v2}")
        for d in r["bad_cd"]:
            print(f"        - {d['기금명']}: {d['조성액']} - {d['사용액']} "
                  f"= {d['조성액']-d['사용액']} ≠ {d['증감액']}")
        if r["total"] is None:
            print("  [3] 개별합=합계행      : 합계행 없음")
        elif r["bad_total"]:
            print("  [3] 개별합=합계행      : 불일치")
            for k, v in r["bad_total"].items():
                if v:
                    print(f"        - {k}: 개별합 {r['sums'][k]:,} vs 합계행 "
                          f"{r['total'][k]:,} (차 {v:+,})")
        else:
            print("  [3] 개별합=합계행      : OK")
        if r["bad_ab"] or r["bad_cd"] or r["bad_total"]:
            ok = False
    print("\n" + ("전체 검산 통과" if ok else "검산 실패 항목 있음 — 위 내역 확인"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
