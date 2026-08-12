"""세입 추계 오차 (17개 광역시도 x 2012~2024).

세입결산: IIBBH — 신규 수집.
당초예산: 이미 받아둔 data/raw/ACEXBG_*.xml — 재호출 없음.

추계오차율 = (세입결산 - 당초예산) / 당초예산 * 100

IIBBH 특성 (명세 확인 결과):
  - 검색인자가 코드 기반: fyr(필수), wa_laf_cd(지역코드), laf_cd(자치단체코드)
    -> HEDFC와 같은 방식. UNIST/ACEXBG의 이름 기반과 다르다.
  - 출력에 wa_laf_cd가 있으므로 본청 판별은 laf_cd == wa_laf_cd
  - total 필드가 세입결산 총계. pfa_amt1~4는 회계별 내역이므로 쓰지 않는다.

산정기준은 총계/결산 최종/통합회계로 ACEXBG(총계/예산 당초/통합회계)와
대상회계·총계 기준이 같아 비교 가능하다.

당초예산은 budget_revision.csv 대신 원본 ACEXBG XML에서 읽는다.
그 CSV는 HEDFC(최종예산) 매칭에 실패한 3행(연기군 2012, 청주시·청원군 2014)을
빼고 만들어졌는데, 추계오차에는 최종예산이 필요 없어 굳이 버릴 이유가 없다.
또 (연도, 자치단체코드)로 조인하는 편이 이름 조인보다 안전하다.

산출물: data/processed/revenue_accuracy.csv
"""

import csv
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv

ENDPOINT_NAME = "IIBBH"
ENDPOINT = f"https://www.lofin365.go.kr/lf/hub/{ENDPOINT_NAME}"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
CSV_PATH = PROCESSED_DIR / "revenue_accuracy.csv"

# IIBBH는 2009~2024지만 당초예산(ACEXBG)이 2012부터라 교집합은 2012~2024.
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


def fetch_page(key: str, year: int, region_code: str, page: int) -> str:
    params = {
        "Key": key,
        "Type": "xml",
        "pIndex": page,
        "pSize": PAGE_SIZE,
        "fyr": str(year),
        "wa_laf_cd": region_code,  # 코드 기반 (HEDFC 방식)
    }
    try:
        resp = requests.get(ENDPOINT, params=params, timeout=60)
    except requests.RequestException as exc:
        raise ApiError(f"{year}/{region_code} p{page} 네트워크 오류: {exc}") from exc
    if resp.status_code != 200:
        raise ApiError(f"{year}/{region_code} p{page} HTTP {resp.status_code}")
    return resp.text


def collect_one(key: str, year: int, region_code: str) -> str:
    main_path = RAW_DIR / f"{ENDPOINT_NAME}_{year}_{region_code}.xml"
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
            raise ApiError(f"{year}/{region_code} p{page} 응답코드 {ecode!r} ({emsg!r})")
        (RAW_DIR / f"{ENDPOINT_NAME}_{year}_{region_code}_p{page}.xml").write_text(
            extra, encoding="utf-8"
        )
        n = len(ET.fromstring(extra).findall("row"))
        if n == 0:
            raise ApiError(f"{year}/{region_code} p{page} 행 0개인데 {got}/{total}")
        got += n

    return "fetched"


def load_initial_budget() -> dict[tuple[str, str], str]:
    """ACEXBG 원본에서 (회계연도, 자치단체코드) -> 당초 세출총계(ane_tott_amt)."""
    out: dict[tuple[str, str], str] = {}
    for path in sorted(RAW_DIR.glob("ACEXBG_*.xml")):
        root = ET.fromstring(path.read_text(encoding="utf-8"))
        for row in root.findall("row"):
            out[(row.findtext("fyr"), row.findtext("laf_cd"))] = row.findtext(
                "ane_tott_amt"
            )
    return out


def build_csv() -> tuple[list[dict], list[tuple]]:
    initial_by_key = load_initial_budget()
    rows: list[dict] = []
    skipped: list[tuple] = []

    for path in sorted(RAW_DIR.glob(f"{ENDPOINT_NAME}_*.xml")):
        root = ET.fromstring(path.read_text(encoding="utf-8"))
        for row in root.findall("row"):
            fyr = row.findtext("fyr")
            laf_cd = row.findtext("laf_cd")
            wa_laf_cd = row.findtext("wa_laf_cd")
            name = row.findtext("laf_hg_nm")

            settled_raw = row.findtext("total")  # 세입결산 총계
            initial_raw = initial_by_key.get((fyr, laf_cd))

            if initial_raw is None:
                skipped.append((fyr, name, "당초예산 없음"))
                continue
            try:
                initial = int(initial_raw)
                settled = int(settled_raw)
            except (TypeError, ValueError):
                skipped.append((fyr, name, "숫자 변환 실패"))
                continue
            if initial == 0:
                skipped.append((fyr, name, "당초예산 0"))
                continue

            diff = settled - initial
            rows.append(
                {
                    "회계연도": fyr,
                    "지역명": row.findtext("wa_laf_hg_nm"),
                    "자치단체명": name,
                    "본청여부": "Y" if laf_cd == wa_laf_cd else "N",
                    "당초예산": initial,
                    "세입결산": settled,
                    "오차액": diff,
                    "오차율": round(diff / initial * 100, 4),
                }
            )

    rows.sort(key=lambda r: (r["회계연도"], r["자치단체명"]))

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "회계연도", "지역명", "자치단체명", "본청여부",
                "당초예산", "세입결산", "오차액", "오차율",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    return rows, skipped


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

    rows, skipped = build_csv()
    print(f"[CSV] {CSV_PATH} — {len(rows)}행")
    if skipped:
        print(f"[제외] {len(skipped)}건: {skipped[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
