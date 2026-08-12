"""예산대비채무비율 전체 수집 (17개 광역시도 x 2009~2024).

행정안전부 지방재정365 OpenAPI (HEDFC).

- 연도 x 지역 루프, 호출 사이 0.5초 sleep
- 응답 XML 원본을 data/raw/HEDFC_{연도}_{지역코드}.xml 로 저장
- 이미 있는 파일은 재호출하지 않고 건너뜀 (재실행 대비)
- list_total_count 기준 페이지네이션
- 예상치 못한 응답코드/네트워크 오류는 즉시 중단 (우회하지 않음)

산출물: data/processed/budget_debt_ratio.csv
"""

import csv
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv

ENDPOINT = "https://www.lofin365.go.kr/lf/hub/HEDFC"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
CSV_PATH = PROCESSED_DIR / "budget_debt_ratio.csv"

YEARS = range(2009, 2025)  # 2009~2024
PAGE_SIZE = 1000
SLEEP_SEC = 0.5

# 17개 광역시도 지역코드.
# 추측이 아니라 wa_laf_cd 없이 fyr=2024 조회한 응답에서 distinct 추출한 값.
# (세종=3200000, 제주=4900000 은 흔한 추측값과 다르므로 주의)
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

SUCCESS_CODE = "INFO-000"
NO_DATA_CODE = "INFO-200"  # 해당 데이터 없음 — 오류가 아니라 결측으로 처리


class ApiError(RuntimeError):
    """예상치 못한 API 응답. 즉시 중단시킨다."""


def load_key() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    key = os.getenv("DATA_GO_KR_KEY", "").strip()
    if not key:
        raise SystemExit(
            "[중단] .env에 DATA_GO_KR_KEY가 없습니다.\n"
            "       키 없이 호출하면 sample key로 동작해 pSize=5/pIndex=1 고정이라 "
            "전체 수집이 불가능합니다."
        )
    return key


def parse_result_code(body: str) -> tuple[str | None, str | None]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None, None
    # 데이터가 없으면 <RESULT>가 루트 자체로 온다. .//RESULT는 루트를 못 잡으므로 별도 처리.
    result = root if root.tag == "RESULT" else root.find(".//RESULT")
    if result is None:
        return None, None
    return result.findtext("CODE"), result.findtext("MESSAGE")


def fetch_page(key: str, year: int, region_code: str, page: int) -> str:
    params = {
        "Key": key,
        "Type": "xml",
        "pIndex": page,
        "pSize": PAGE_SIZE,
        "fyr": str(year),
        "wa_laf_cd": region_code,
    }
    try:
        resp = requests.get(ENDPOINT, params=params, timeout=60)
    except requests.RequestException as exc:
        raise ApiError(f"{year}/{region_code} p{page} 네트워크 오류: {exc}") from exc

    if resp.status_code != 200:
        raise ApiError(f"{year}/{region_code} p{page} HTTP {resp.status_code}")

    return resp.text


def collect_one(key: str, year: int, region_code: str) -> str:
    """한 (연도, 지역) 조합 수집. 이미 저장돼 있으면 건너뛴다."""
    main_path = RAW_DIR / f"HEDFC_{year}_{region_code}.xml"
    if main_path.exists():
        return "skipped"

    body = fetch_page(key, year, region_code, 1)
    code, message = parse_result_code(body)

    if code == NO_DATA_CODE:
        main_path.write_text(body, encoding="utf-8")
        return "nodata"

    if code != SUCCESS_CODE:
        raise ApiError(
            f"{year}/{region_code} 예상치 못한 응답코드 {code!r} ({message!r})\n"
            f"--- 응답 앞부분 ---\n{body[:800]}"
        )

    main_path.write_text(body, encoding="utf-8")

    # 페이지네이션: list_total_count가 받은 행 수보다 크면 추가 페이지 요청
    root = ET.fromstring(body)
    total = int(root.findtext(".//list_total_count") or 0)
    got = len(root.findall("row"))
    page = 1
    while got < total:
        page += 1
        time.sleep(SLEEP_SEC)
        extra = fetch_page(key, year, region_code, page)
        ecode, emsg = parse_result_code(extra)
        if ecode != SUCCESS_CODE:
            raise ApiError(
                f"{year}/{region_code} p{page} 응답코드 {ecode!r} ({emsg!r})"
            )
        (RAW_DIR / f"HEDFC_{year}_{region_code}_p{page}.xml").write_text(
            extra, encoding="utf-8"
        )
        n = len(ET.fromstring(extra).findall("row"))
        if n == 0:
            raise ApiError(
                f"{year}/{region_code} p{page} 행이 0개인데 {got}/{total} 밖에 못 받음"
            )
        got += n

    return "fetched"


def iter_raw_files():
    """저장된 원본 XML을 모두 순회 (본 페이지 + _p2 이후 페이지 포함)."""
    return sorted(RAW_DIR.glob("HEDFC_*.xml"))


def build_csv() -> list[dict]:
    rows = []
    for path in iter_raw_files():
        root = ET.fromstring(path.read_text(encoding="utf-8"))
        for row in root.findall("row"):
            laf_cd = row.findtext("laf_cd")
            wa_laf_cd = row.findtext("wa_laf_cd")
            rows.append(
                {
                    "회계연도": row.findtext("fyr"),
                    "지역명": row.findtext("wa_laf_hg_nm"),
                    "자치단체명": row.findtext("laf_hg_nm"),
                    "자치단체코드": laf_cd,
                    # 본청 판별: 자치단체코드 == 지역코드 인 행이 광역 본청
                    "본청여부": "Y" if laf_cd == wa_laf_cd else "N",
                    "예산대비채무비율": row.findtext("rate"),
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
                "예산대비채무비율",
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
                status = collect_one(key, year, region_code)
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