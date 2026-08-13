"""도별 재정공시에서 기금운영현황 표 수집 (2021~2024 회계연도).

경기는 별도 스크립트(extract_gg_funds.py)로 이미 처리했다. 여기서는 나머지 도.

동작:
  1) src/disclosure_urls.py 의 PAGES[시도][회계연도] 페이지를 받는다
  2) 첨부 링크를 라벨과 함께 뽑고, 기금운영현황 > 공통공시 > 결산전체 순으로 후보를 고른다
  3) 후보를 순서대로 내려받아 형식을 판별한다 (HWPX 우선, 없으면 PDF, 구형 hwp는 건너뜀)
  4) 기금표를 찾아 셀 단위로 추출하고 검산한다

원자료 보존:
  - 기금명·계정 구분은 원문 그대로 둔다 (경기 2계정 / 강원·충남 1계정 / 경북 무계정)
  - 통합재정안정화기금여부 컬럼(Y/N)만 덧붙여 집계 시 합산할 수 있게 한다

단위는 표 위치 기준 최근접 '(단위 : X)' 로 판정하고 원본단위 컬럼에 남긴다.
음수는 △ 와 - 둘 다 처리한다. 금액칸의 '-' 는 0이다.

산출물: data/processed/province_fund_detail.csv
"""

import csv
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from disclosure_urls import PAGES, FILES, UNRESOLVED  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOC_DIR = PROJECT_ROOT / "data" / "raw" / "province_disclosure"
CSV_PATH = PROJECT_ROOT / "data" / "processed" / "province_fund_detail.csv"

YEARS = (2021, 2022, 2023, 2024)
UNIT_TO_MILLION = {"백만원": 1, "억원": 100, "천원": 0.001}

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

ln = lambda t: t.rsplit("}", 1)[-1]


# ---------------- 공통 유틸 ----------------
def norm_num(s):
    if s is None:
        return None
    t = s.strip().replace(",", "").replace(" ", "")
    if t == "-":
        return 0
    t = t.replace("△", "-").replace("▲", "-").replace("−", "-").replace("–", "-")
    if t == "":
        return None
    return int(t) if re.fullmatch(r"-?\d+", t) else None


# '단위 : 백만 원' 처럼 글자 사이에 공백(전각 U+3000 포함)이 끼는 표기가 있다.
# 강원 FY2023이 '(단위 : 백만 원)'이라 '백만원'만 찾던 이전 정규식이 못 잡고
# 근처의 '억원'을 집어 값이 100배로 부풀었다. 공백을 모두 흡수한다.
UNIT_RE = re.compile(r"단위[\s:：]*((?:백\s*만|억|천)\s*원|원)\s*\)")


def find_unit(text: str):
    anchors = [m.start() for m in re.finditer("일몰기간", text)]
    if not anchors:
        anchors = [m.start() for m in re.finditer("조성액", text)]
    if not anchors:
        return None
    cands = [(m.start(), re.sub(r"\s+", "", m.group(1))) for m in UNIT_RE.finditer(text)]
    if not cands:
        return None
    pos, unit = min(cands, key=lambda c: abs(c[0] - anchors[0]))
    return unit if abs(pos - anchors[0]) <= 8000 else None


def sniff(b: bytes) -> str:
    if b[:4] == b"%PDF":
        return "pdf"
    if b[:4] == b"PK\x03\x04":
        return "hwpx"
    if b[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "hwp"
    return "unknown"


def is_fund_table(txt: str) -> bool:
    return ("조성액" in txt and "사용액" in txt) or "통합재정안정화" in txt


# ---------------- 표 추출 ----------------
def rows_from_hwpx(path: Path):
    z = zipfile.ZipFile(path)
    for name in [n for n in z.namelist() if re.search(r"section\d+\.xml$", n)]:
        raw = z.read(name).decode("utf-8", "replace")
        flat = re.sub(r"<[^>]+>", "", raw)
        if "조성액" not in flat:
            continue
        root = ET.fromstring(raw)
        for tbl in (e for e in root.iter() if ln(e.tag) == "tbl"):
            txt = "".join(e.text or "" for e in tbl.iter() if ln(e.tag) == "t")
            if not is_fund_table(txt):
                continue
            out = []
            for tr in [e for e in tbl.iter() if ln(e.tag) == "tr"]:
                row = {}
                for tc in [e for e in tr.iter() if ln(e.tag) == "tc"]:
                    addr = [e for e in tc.iter() if ln(e.tag) == "cellAddr"]
                    col = int(addr[0].get("colAddr")) if addr else -1
                    row[col] = "".join(
                        e.text or "" for e in tc.iter() if ln(e.tag) == "t"
                    ).strip()
                if len(row) >= 6:
                    out.append([row.get(i, "") for i in range(7)])
            if out:
                return out, find_unit(flat)
    return None, None


def rows_from_pdf(path: Path):
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        for pg in pdf.pages:
            text = pg.extract_text() or ""
            if not is_fund_table(text):
                continue
            for tb in pg.extract_tables():
                flat = " ".join(c or "" for r in tb for c in r)
                if not is_fund_table(flat):
                    continue
                out = [[(c or "").replace("\n", "") for c in r] for r in tb
                       if len(r) >= 6]
                if out:
                    return out, find_unit(text)
    return None, None


def parse_rows(rows):
    data, total = [], None
    for r in rows:
        name = re.sub(r"\s+", " ", (r[0] or "")).strip()
        if not name or "구 분" in name or "종류별" in name or name.startswith("(단위"):
            continue
        nums = [norm_num(r[k]) for k in (1, 2, 3, 4, 5)]
        # 금액칸의 빈 셀은 0이다('-'와 동일). 강원 고향사랑기금·충남 국외소재문화재기금이
        # 사용액을 공백으로 두어 행 전체가 버려졌고, 합계 검산에서 발각됐다.
        # 다만 헤더 잔여물을 숫자행으로 오인하지 않도록 최소 3칸은 실제 숫자여야 한다.
        if sum(v is not None for v in nums) < 3:
            continue
        nums = [0 if v is None else v for v in nums]
        rec = {"기금명": name, "전년도현재액": nums[0], "증감액": nums[1],
               "조성액": nums[2], "사용액": nums[3], "당해현재액": nums[4],
               "일몰기간": re.sub(r"\s+", "", (r[6] if len(r) > 6 else "") or "")}
        if name.replace(" ", "") in ("합계", "계", "총계"):
            total = rec
        else:
            data.append(rec)
    return data, total


# ---------------- 첨부 후보 ----------------
def candidates(page_url: str):
    r = S.get(page_url, timeout=90)
    r.encoding = r.apparent_encoding or r.encoding
    html = r.text
    out = []
    for m in re.finditer(r'href="([^"]*(?:download[^"]*|\.hwpx|\.hwp|\.pdf))"', html, re.I):
        raw = unescape(m.group(1))
        if raw.lower().startswith("javascript:") or raw.startswith("#"):
            continue
        # './foo.do?...' 같은 상대경로도 처리해야 한다 (충북이 이 형태).
        href = urljoin(str(r.url), raw)
        pre = unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ",
                                                  html[max(0, m.start() - 1500):m.start()])))
        out.append((href, pre[-120:]))
    def score(item):
        href, lab = item
        hay = href + " " + lab
        s = 0
        if "기금운영현황" in hay:
            s -= 200
        if "공통공시" in hay:
            s -= 100
        if "재정공시" in href:
            s -= 60
        if "결산" in hay:
            s -= 20
        # 별지/별첨/부속 자료는 기금표가 없다
        if re.search(r"별지|별첨|집행내역|국외여비|보조금|성과평가|원가회계", hay):
            s += 150
        if re.search(r"\.xlsx?($|[?&])", href, re.I):
            s += 200
        if "hwpx" in href.lower():
            s -= 10
        if re.search(r"\.hwp($|[?&])", href, re.I):
            s += 30
        return s
    out.sort(key=score)
    return out


def collect_one(sido: str, fy: int):
    """반환: (data, total, unit, src, note)"""
    direct = FILES.get(sido, {}).get(fy)
    if direct:
        cands = [(u, "직접지정") for u in direct]
    else:
        page = PAGES[sido][fy]
        try:
            cands = candidates(page)
        except Exception as exc:
            return None, None, None, None, f"페이지 접근 실패: {type(exc).__name__}"
        if not cands:
            return None, None, None, None, "첨부 링크 없음"

    tried = []
    for href, lab in cands[:8]:
        key = re.sub(r"[^0-9A-Za-z가-힣]", "_", href)[-60:]
        local = DOC_DIR / f"{sido}_{fy}_{key}"
        try:
            if local.exists() and local.stat().st_size > 0:
                blob = local.read_bytes()
            else:
                resp = S.get(href, timeout=240)
                if resp.status_code != 200 or len(resp.content) < 2000:
                    tried.append(f"{href[-30:]} HTTP{resp.status_code}/{len(resp.content)}B")
                    continue
                blob = resp.content
                local.write_bytes(blob)
        except Exception as exc:
            tried.append(f"{href[-30:]} {type(exc).__name__}")
            continue

        kind = sniff(blob)
        if kind == "hwp":
            tried.append(f"{href[-30:]} 구형hwp")
            continue
        if kind == "unknown":
            tried.append(f"{href[-30:]} 형식불명")
            continue

        path = local.with_suffix("." + kind)
        if not path.exists():
            path.write_bytes(blob)
        try:
            rows, unit = rows_from_hwpx(path) if kind == "hwpx" else rows_from_pdf(path)
        except Exception as exc:
            tried.append(f"{href[-30:]} 파싱오류 {type(exc).__name__}")
            continue
        if not rows:
            tried.append(f"{href[-30:]} {kind} 기금표없음")
            continue
        data, total = parse_rows(rows)
        if not data:
            tried.append(f"{href[-30:]} {kind} 행파싱실패")
            continue
        return data, total, unit, f"{kind}:{href[-40:]}", "; ".join(tried[:3])
    return None, None, None, None, "후보 소진 | " + "; ".join(tried[:5])


def main() -> int:
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    all_rows, report = [], []

    for sido in PAGES:
        line = []
        for fy in sorted(PAGES[sido]):
            data, total, unit, src, note = collect_one(sido, fy)
            rec = {"sido": sido, "fy": fy, "unit": unit, "src": src, "note": note,
                   "n": len(data) if data else 0, "bad_ab": [], "bad_cd": [],
                   "bad_total": None, "total": total}
            if data:
                for d in data:
                    e1 = d["전년도현재액"] + d["증감액"] - d["당해현재액"]
                    if e1:
                        rec["bad_ab"].append(f"{d['기금명']}({e1:+d})")
                    e2 = d["조성액"] - d["사용액"] - d["증감액"]
                    if e2:
                        rec["bad_cd"].append(f"{d['기금명']}({e2:+d})")
                if total:
                    sums = {k: sum(x[k] for x in data) for k in
                            ("전년도현재액", "증감액", "조성액", "사용액", "당해현재액")}
                    diff = {k: sums[k] - total[k] for k in sums}
                    if any(v for v in diff.values()):
                        rec["bad_total"] = diff
                mult = UNIT_TO_MILLION.get(unit)
                if mult is None:
                    rec["note"] = (rec["note"] + " | 단위판정실패").strip(" |")
                for d in data:
                    all_rows.append({
                        "시도명": sido, "회계연도": fy, "기금명": d["기금명"],
                        "전년도현재액": d["전년도현재액"] * (mult or 1),
                        "증감액": d["증감액"] * (mult or 1),
                        "조성액": d["조성액"] * (mult or 1),
                        "사용액": d["사용액"] * (mult or 1),
                        "당해현재액": d["당해현재액"] * (mult or 1),
                        "일몰기간": d["일몰기간"],
                        "원본단위": unit or "판정실패",
                        "통합재정안정화기금여부":
                            "Y" if "통합재정안정화" in d["기금명"].replace(" ", "") else "N",
                    })
            report.append(rec)
            line.append(f"{fy} {'O' if data else 'X'}({unit or '-'})")
        chk = all(not r["bad_ab"] and not r["bad_cd"] and not r["bad_total"]
                  for r in report if r["sido"] == sido and r["n"])
        got = sum(1 for r in report if r["sido"] == sido and r["n"])
        idx = list(PAGES).index(sido) + 1
        print(f"[{idx}/{len(PAGES)}] {sido} — " + " / ".join(line) +
              f" / 검산 {'통과' if chk and got else '실패' if got else '해당없음'}", flush=True)

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "시도명", "회계연도", "기금명", "전년도현재액", "증감액", "조성액",
            "사용액", "당해현재액", "일몰기간", "원본단위", "통합재정안정화기금여부"])
        w.writeheader()
        w.writerows(all_rows)
    print(f"\n[CSV] {CSV_PATH} — {len(all_rows)}행")

    print("\n" + "=" * 70 + "\n상세")
    for r in report:
        st = f"{r['n']}개" if r["n"] else "실패"
        print(f"  {r['sido']} FY{r['fy']}: {st} | 단위={r['unit']} | {r['src']}")
        if r["note"]:
            print(f"      note: {r['note'][:150]}")
        if r["bad_ab"]:
            print(f"      검산1 실패 {len(r['bad_ab'])}건: {r['bad_ab'][:6]}")
        if r["bad_cd"]:
            print(f"      검산2 실패 {len(r['bad_cd'])}건: {r['bad_cd'][:6]}")
        if r["bad_total"]:
            print(f"      검산3 불일치: { {k: v for k, v in r['bad_total'].items() if v} }")
    if UNRESOLVED:
        print(f"\n[미해결 시도] {list(UNRESOLVED)} — 결산 페이지 URL 미확보")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
