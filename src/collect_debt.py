"""지방채 잔액(ACCAM) + 기금현재액(DJAIH) 수집.

카탈로그 146종 전수 확인 결과, 요청 5종 중 실제로 존재하는 것은 2종뿐이다.
  O 지방채 잔액        -> ACCAM  '회계별 지방채 잔액'  2000~2024
  O 기금 잔액          -> DJAIH  '기금현재액'          2014~2024
  X 지방채 '발행액'     -> 없음 (잔액만 제공)
  X 발행 한도액/소진율   -> 없음
  X 채무부담행위        -> 없음

두 엔드포인트도 서로 체계가 다르다:

  ACCAM: 검색인자 fyr(필수), laf_cd(선택). **지역 필터(wa_laf_cd)가 없다.**
         출력에도 wa_laf_cd가 없어 본청 판별은 laf_cd ∈ 17개 지역코드.
         금액 필드는 lgfd_ramt_amt1~4 (일반/기타특별/공기업특별/기금).
         산정기준 결산, 대상회계 일반+공기업특별+기타특별+기금.

  DJAIH: 검색인자 fyr(필수), wa_laf_cd(선택), laf_cd(선택).
         출력에 wa_laf_cd가 있어 laf_cd == wa_laf_cd 로 본청 판별.
         pfa_amt1 전년도현재액 / 2 증감액총계 / 3 조성액 / 4 사용액 / 5 당해년도현재액.
         산정기준 결산, 대상회계 기금회계.
         **기금 종류별 구분이 없다.** 자치단체 전체 기금의 합계라
         통합재정안정화기금만 따로 볼 수는 없다.

둘 다 지역 필터 없이 fyr만으로 전량 수신되므로 연도당 1회 호출.

산출물:
  data/processed/local_debt_balance.csv
  data/processed/fund_balance.csv
"""

import csv
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

PAGE_SIZE = 1000
SLEEP_SEC = 0.5

REGIONS = {
    "1100000": "서울", "2600000": "부산", "2700000": "대구", "2800000": "인천",
    "2900000": "광주", "3000000": "대전", "3100000": "울산", "3200000": "세종",
    "4100000": "경기", "4200000": "강원", "4300000": "충북", "4400000": "충남",
    "4500000": "전북", "4600000": "전남", "4700000": "경북", "4800000": "경남",
    "4900000": "제주",
}
REGION_CODES = set(REGIONS)

SUCCESS_CODE = "INFO-000"
NO_DATA_CODE = "INFO-200"

SPECS = {
    "ACCAM": {
        "years": range(2000, 2025),
        "csv": "local_debt_balance.csv",
        "amounts": [
            ("일반회계", "lgfd_ramt_amt1"),
            ("기타특별회계", "lgfd_ramt_amt2"),
            ("공기업특별회계", "lgfd_ramt_amt3"),
            ("기금회계", "lgfd_ramt_amt4"),
        ],
        "total_col": "지방채잔액합계",
        "hq_by_code_set": True,   # wa_laf_cd 출력 없음
    },
    "DJAIH": {
        "years": range(2014, 2025),
        "csv": "fund_balance.csv",
        "amounts": [
            ("전년도현재액", "pfa_amt1"),
            ("증감액총계", "pfa_amt2"),
            ("조성액", "pfa_amt3"),
            ("사용액", "pfa_amt4"),
            ("당해년도현재액", "pfa_amt5"),
        ],
        "total_col": None,
        "hq_by_code_set": False,  # laf_cd == wa_laf_cd
    },
}


class ApiError(RuntimeError):
    """예상치 못한 API 응답. 즉시 중단시킨다."""


def load_key() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    key = os.getenv("DATA_GO_KR_KEY", "").strip()
    if not key:
        raise SystemExit("[중단] .env에 DATA_GO_KR_KEY가 없습니다.")
    return key


def parse_result_code(body: str) -> tuple[str | None, str | None]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None, None
    result = root if root.tag == "RESULT" else root.find(".//RESULT")
    if result is None:
        return None, None
    return result.findtext("CODE"), result.findtext("MESSAGE")


def fetch_page(key: str, ep: str, year: int, page: int) -> str:
    params = {
        "Key": key, "Type": "xml", "pIndex": page,
        "pSize": PAGE_SIZE, "fyr": str(year),
    }
    try:
        resp = requests.get(
            f"https://www.lofin365.go.kr/lf/hub/{ep}", params=params, timeout=60
        )
    except requests.RequestException as exc:
        raise ApiError(f"{ep} {year} p{page} 네트워크 오류: {exc}") from exc
    if resp.status_code != 200:
        raise ApiError(f"{ep} {year} p{page} HTTP {resp.status_code}")
    return resp.text


def collect_one(key: str, ep: str, year: int) -> str:
    main_path = RAW_DIR / f"{ep}_{year}.xml"
    if main_path.exists():
        return "skipped"

    body = fetch_page(key, ep, year, 1)
    code, message = parse_result_code(body)
    if code == NO_DATA_CODE:
        main_path.write_text(body, encoding="utf-8")
        return "nodata"
    if code != SUCCESS_CODE:
        raise ApiError(
            f"{ep} {year} 예상치 못한 응답코드 {code!r} ({message!r})\n"
            f"--- 응답 앞부분 ---\n{body[:800]}"
        )
    main_path.write_text(body, encoding="utf-8")

    root = ET.fromstring(body)
    total = int(root.findtext(".//list_total_count") or 0)
    got = len(root.findall("row"))
    page = 1
    while got < total:
        page += 1
        time.sleep(SLEEP_SEC)
        extra = fetch_page(key, ep, year, page)
        ecode, emsg = parse_result_code(extra)
        if ecode != SUCCESS_CODE:
            raise ApiError(f"{ep} {year} p{page} 응답코드 {ecode!r} ({emsg!r})")
        (RAW_DIR / f"{ep}_{year}_p{page}.xml").write_text(extra, encoding="utf-8")
        n = len(ET.fromstring(extra).findall("row"))
        if n == 0:
            raise ApiError(f"{ep} {year} p{page} 행 0개인데 {got}/{total}")
        got += n
    return "fetched"


def to_int(text: str | None) -> int:
    if text is None or not text.strip():
        return 0
    return int(float(text.replace(",", "")))


def build_csv(ep: str) -> int:
    spec = SPECS[ep]
    rows = []
    for path in sorted(RAW_DIR.glob(f"{ep}_*.xml")):
        root = ET.fromstring(path.read_text(encoding="utf-8"))
        for row in root.findall("row"):
            laf_cd = row.findtext("laf_cd")
            if spec["hq_by_code_set"]:
                is_hq = laf_cd in REGION_CODES
            else:
                is_hq = laf_cd == row.findtext("wa_laf_cd")

            rec = {
                "회계연도": row.findtext("fyr"),
                "지역명": row.findtext("wa_laf_hg_nm"),
                "자치단체명": row.findtext("laf_hg_nm"),
                "자치단체코드": laf_cd,
                "본청여부": "Y" if is_hq else "N",
            }
            for label, field in spec["amounts"]:
                rec[label] = to_int(row.findtext(field))
            if spec["total_col"]:
                rec[spec["total_col"]] = sum(
                    rec[label] for label, _ in spec["amounts"]
                )
            rows.append(rec)

    rows.sort(key=lambda r: (r["회계연도"], r["자치단체코드"] or ""))
    fields = ["회계연도", "지역명", "자치단체명", "자치단체코드", "본청여부"]
    fields += [label for label, _ in spec["amounts"]]
    if spec["total_col"]:
        fields.append(spec["total_col"])

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROCESSED_DIR / spec["csv"]
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"[CSV] {out} — {len(rows)}행")
    return len(rows)


def main() -> int:
    key = load_key()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    for ep, spec in SPECS.items():
        stats = {"fetched": 0, "skipped": 0, "nodata": 0}
        nodata = []
        print(f"\n=== {ep} ({spec['years'][0]}~{spec['years'][-1]}) ===")
        for year in spec["years"]:
            try:
                status = collect_one(key, ep, year)
            except ApiError as exc:
                print(f"\n[중단] {exc}", file=sys.stderr)
                return 1
            stats[status] += 1
            if status == "nodata":
                nodata.append(year)
            if status == "fetched":
                time.sleep(SLEEP_SEC)
        print(f"  {stats}")
        if nodata:
            print(f"  [데이터 없음] {nodata}")
        build_csv(ep)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
