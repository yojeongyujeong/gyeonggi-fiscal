"""당초예산 대비 최종예산 증감률 (17개 광역시도 x 2012~2024).

당초: ACEXBG(세출예산, 당초) — 신규 수집.
최종: 이미 받아둔 data/raw/HEDFC_*.xml 의 pfa_amt2(최종예산액) — 재호출 없음.

ACEXBG 특성 (명세 확인 결과):
  - 검색인자가 이름 기반: fyr(필수), wa_laf_hg_nm(지역명), laf_hg_nm(자치단체명)
  - 출력에는 wa_laf_cd가 있으므로 본청 판별은 laf_cd == wa_laf_cd 로 한다
  - 세출총계 필드명은 ane_tott_amt (HEDFC/UNIST의 pfa_amt* 방식과 다름)

두 지표 모두 총계·통합회계라 비교 가능. 다만 HEDFC pfa_amt2가 세입 기준인지
세출 기준인지는 명세에 없다.

산출물: data/processed/budget_revision.csv
"""

import csv
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv

ENDPOINT_NAME = "ACEXBG"
ENDPOINT = f"https://www.lofin365.go.kr/lf/hub/{ENDPOINT_NAME}"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
CSV_PATH = PROCESSED_DIR / "budget_revision.csv"

# ACEXBG는 2012~2026이나 HEDFC(최종)가 2024까지라 교집합은 2012~2024.
YEARS = range(2012, 2025)
PAGE_SIZE = 1000
SLEEP_SEC = 0.5

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
            raise ApiError(f"{year}/{region_name} p{page} 행 0개인데 {got}/{total}")
        got += n

    return "fetched"


def load_final_budget() -> dict[tuple[str, str], str]:
    """HEDFC 원본에서 (회계연도, 자치단체코드) -> 최종예산액(pfa_amt2)."""
    out: dict[tuple[str, str], str] = {}
    for path in sorted(RAW_DIR.glob("HEDFC_*.xml")):
        root = ET.fromstring(path.read_text(encoding="utf-8"))
        for row in root.findall("row"):
            out[(row.findtext("fyr"), row.findtext("laf_cd"))] = row.findtext("pfa_amt2")
    return out


def build_csv() -> tuple[list[dict], list[tuple[str, str, str]]]:
    final_by_key = load_final_budget()
    rows: list[dict] = []
    unmatched: list[tuple[str, str, str]] = []

    for path in sorted(RAW_DIR.glob(f"{ENDPOINT_NAME}_*.xml")):
        root = ET.fromstring(path.read_text(encoding="utf-8"))
        for row in root.findall("row"):
            fyr = row.findtext("fyr")
            laf_cd = row.findtext("laf_cd")
            wa_laf_cd = row.findtext("wa_laf_cd")
            initial_raw = row.findtext("ane_tott_amt")  # 세출총계(당초)
            final_raw = final_by_key.get((fyr, laf_cd))

            if final_raw is None:
                unmatched.append((fyr, laf_cd, row.findtext("laf_hg_nm")))
                continue

            try:
                initial = int(initial_raw)
                final = int(final_raw)
            except (TypeError, ValueError):
                unmatched.append((fyr, laf_cd, row.findtext("laf_hg_nm")))
                continue

            if initial == 0:
                unmatched.append((fyr, laf_cd, row.findtext("laf_hg_nm")))
                continue

            diff = final - initial
            rows.append(
                {
                    "회계연도": fyr,
                    "지역명": row.findtext("wa_laf_hg_nm"),
                    "자치단체명": row.findtext("laf_hg_nm"),
                    "본청여부": "Y" if laf_cd == wa_laf_cd else "N",
                    "당초예산": initial,
                    "최종예산": final,
                    "증감액": diff,
                    "증감률": round(diff / initial * 100, 4),
                }
            )

    rows.sort(key=lambda r: (r["회계연도"], r["자치단체명"]))

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "회계연도", "지역명", "자치단체명", "본청여부",
                "당초예산", "최종예산", "증감액", "증감률",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    return rows, unmatched


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
                print(f"[중단] {done-1}/{total_combos} 완료 후 멈춤.", file=sys.stderr)
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

    rows, unmatched = build_csv()
    print(f"[CSV] {CSV_PATH} — {len(rows)}행")
    if unmatched:
        print(f"[매칭 실패] {len(unmatched)}건 (최종예산 없음/당초 0): {unmatched[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
