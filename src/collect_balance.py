"""통합재정수지비율[결산] 전체 수집 (17개 광역시도 x 2016~2024).

행정안전부 지방재정365 OpenAPI (UNIST). collect.py(HEDFC)와 같은 구조.

HEDFC와의 차이 — 명세에서 확인한 사실:
  1) 엔드포인트가 /lf/hub/UNIST
  2) 검색 요청인자가 코드가 아니라 '이름'이다:
       wa_laf_hg_nm (지역명), laf_hg_nm (자치단체명)
     -> HEDFC의 wa_laf_cd(지역코드)는 이 API에서 쓸 수 없다.
        조회는 지역명으로 하고, 파일명만 지역코드로 붙인다.
  3) 출력값에 wa_laf_cd가 없다.
     -> 'laf_cd == wa_laf_cd' 본청 판별이 불가능.
        대신 laf_cd가 17개 광역 지역코드에 속하는지로 판별한다.
        (2024년 기준 정확히 17건, laf_hg_nm이 '본청'으로 끝나는 행과 완전 일치 확인)
  4) 값이 음수일 수 있다 (적자). 부호를 그대로 보존한다.

산출물: data/processed/consolidated_balance_ratio.csv
"""

import csv
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv

ENDPOINT_NAME = "UNIST"
ENDPOINT = f"https://www.lofin365.go.kr/lf/hub/{ENDPOINT_NAME}"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
CSV_PATH = PROCESSED_DIR / "consolidated_balance_ratio.csv"

# 보유연도 2016~2024. 경계 확인: fyr=2015 / fyr=2025 모두 INFO-200(데이터 없음).
YEARS = range(2016, 2025)
PAGE_SIZE = 1000
SLEEP_SEC = 0.5

# collect.py와 동일한 17개 지역코드. 여기서는 조회 키가 아니라
# (a) 파일명, (b) 본청 판별용으로 쓴다.
REGIONS = {
    "1100000": "서울",
    "2600000": "부산",
    "2700000": "대구",
    "2800000": "인천",
    "2900000": "광주",
    "3000000": "대전",
    "3100000": "울산",
    "3200000": "세종",
    "4100000": "경기",
    "4200000": "강원",
    "4300000": "충북",
    "4400000": "충남",
    "4500000": "전북",
    "4600000": "전남",
    "4700000": "경북",
    "4800000": "경남",
    "4900000": "제주",
}
REGION_CODES = set(REGIONS)

SUCCESS_CODE = "INFO-000"
NO_DATA_CODE = "INFO-200"


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
    # 데이터가 없으면 <RESULT>가 루트 자체로 온다.
    result = root if root.tag == "RESULT" else root.find(".//RESULT")
    if result is None:
        return None, None
    return result.findtext("CODE"), result.findtext("MESSAGE")


def fetch_page(key: str, year: int, region_name: str, page: int) -> str:
    params = {
        "Key": key,
        "Type": "xml",
        "pIndex": page,
        "pSize": PAGE_SIZE,
        "fyr": str(year),
        "wa_laf_hg_nm": region_name,  # 코드가 아니라 지역명
    }
    try:
        resp = requests.get(ENDPOINT, params=params, timeout=60)
    except requests.RequestException as exc:
        raise ApiError(f"{year}/{region_name} p{page} 네트워크 오류: {exc}") from exc

    if resp.status_code != 200:
        raise ApiError(f"{year}/{region_name} p{page} HTTP {resp.status_code}")

    return resp.text


def collect_one(key: str, year: int, region_code: str, region_name: str) -> str:
    main_path = RAW_DIR / f"{ENDPOINT_NAME}_{year}_{region_code}.xml"
    if main_path.exists():
        return "skipped"

    body = fetch_page(key, year, region_name, 1)
    code, message = parse_result_code(body)

    if code == NO_DATA_CODE:
        main_path.write_text(body, encoding="utf-8")
        return "nodata"

    if code != SUCCESS_CODE:
        raise ApiError(
            f"{year}/{region_name} 예상치 못한 응답코드 {code!r} ({message!r})\n"
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
        extra = fetch_page(key, year, region_name, page)
        ecode, emsg = parse_result_code(extra)
        if ecode != SUCCESS_CODE:
            raise ApiError(f"{year}/{region_name} p{page} 응답코드 {ecode!r} ({emsg!r})")
        (RAW_DIR / f"{ENDPOINT_NAME}_{year}_{region_code}_p{page}.xml").write_text(
            extra, encoding="utf-8"
        )
        n = len(ET.fromstring(extra).findall("row"))
        if n == 0:
            raise ApiError(
                f"{year}/{region_name} p{page} 행이 0개인데 {got}/{total} 밖에 못 받음"
            )
        got += n

    return "fetched"


def build_csv() -> list[dict]:
    rows = []
    for path in sorted(RAW_DIR.glob(f"{ENDPOINT_NAME}_*.xml")):
        root = ET.fromstring(path.read_text(encoding="utf-8"))
        for row in root.findall("row"):
            laf_cd = row.findtext("laf_cd")
            rows.append(
                {
                    "회계연도": row.findtext("fyr"),
                    "지역명": row.findtext("wa_laf_hg_nm"),
                    "자치단체명": row.findtext("laf_hg_nm"),
                    "자치단체코드": laf_cd,
                    # 출력에 wa_laf_cd가 없으므로 17개 지역코드 포함 여부로 판별
                    "본청여부": "Y" if laf_cd in REGION_CODES else "N",
                    # 음수 부호 그대로 보존 (문자열 원문 유지)
                    "통합재정수지비율": row.findtext("rate"),
                }
            )

    rows.sort(key=lambda r: (r["회계연도"], r["자치단체코드"]))

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "회계연도",
                "지역명",
                "자치단체명",
                "자치단체코드",
                "본청여부",
                "통합재정수지비율",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    return rows


def main() -> int:
    key = load_key()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    stats = {"fetched": 0, "skipped": 0, "nodata": 0}
    nodata_pairs = []
    total_combos = len(YEARS) * len(REGIONS)
    done = 0

    for year in YEARS:
        for region_code, region_name in REGIONS.items():
            done += 1
            try:
                status = collect_one(key, year, region_code, region_name)
            except ApiError as exc:
                print(f"\n[중단] {exc}", file=sys.stderr)
                print(
                    f"[중단] {done-1}/{total_combos} 완료 후 멈춤. "
                    "저장된 파일은 남아있으므로 재실행 시 이어서 진행됩니다.",
                    file=sys.stderr,
                )
                return 1

            stats[status] += 1
            if status == "nodata":
                nodata_pairs.append((year, region_name))
            if status == "fetched":
                time.sleep(SLEEP_SEC)

            if done % 25 == 0 or done == total_combos:
                print(f"  진행 {done}/{total_combos} {stats}")

    print(f"\n[수집] {stats}")
    if nodata_pairs:
        print(f"[데이터 없음] {len(nodata_pairs)}건: {nodata_pairs}")

    rows = build_csv()
    print(f"[CSV] {CSV_PATH} — {len(rows)}행")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
