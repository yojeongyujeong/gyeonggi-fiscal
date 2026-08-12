"""도시군세 세목별 비중 (KAAAE) 2013~2024 수집.

**주의: 이 데이터셋은 순계·결산·일반회계다.**
기존 지표들(HEDFC/ACEXBG/IIBBH — 총계·통합회계)과 같은 표에 놓지 말 것.
취득세 의존도 계산 용도로만 쓴다.

KAAAE 특성 (명세 확인 결과):
  - 검색인자가 fyr 하나뿐. 지역 필터가 없어 연도당 1회 호출로 전량 수신.
  - 시도코드가 2자리(서울=11)이고 시도명이 전체 명칭(서울특별시)이다.
    기존 7자리 코드/약칭과 다르므로 아래 SIDO_MAP으로 매핑한다.
  - cap_dv_nm 이 '시도' / '시' / '군' 세 층. 본청 개념은 없다.
  - 출력 rate(비중)는 전국 대비 비중이므로, 시도별 의존도는 직접 계산해야 한다.

**cap_dv_nm='시도'만 뽑으면 안 된다.** 도 지역의 '시도' 행에는 도세
(취득세·등록면허세·레저세·지방교육세·지역자원시설세)만 있고, 시군세
(재산세·자동차세·지방소득세·담배소비세·주민세)는 '시'/'군' 행에 따로 있다.
반면 특별시·광역시의 '시도' 행에는 12개 세목이 전부 들어온다.
따라서 '시도'만 쓰면 도의 분모가 작아져 취득세 의존도가 구조적으로 부풀려진다.
지역 전체 지방세는 시도+시+군 을 합산해야 한다.

산출물: data/processed/tax_composition.csv  (전 계층, '계층' 컬럼으로 구분)
"""

import csv
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv

ENDPOINT_NAME = "KAAAE"
ENDPOINT = f"https://www.lofin365.go.kr/lf/hub/{ENDPOINT_NAME}"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
CSV_PATH = PROCESSED_DIR / "tax_composition.csv"

YEARS = range(2013, 2025)
PAGE_SIZE = 1000
SLEEP_SEC = 0.5

# KAAAE 전체 명칭 -> (기존 약칭, 기존 7자리 지역코드).
# 강원/전북은 특별자치도 개편으로 명칭이 바뀌므로 변형을 모두 넣는다.
SIDO_MAP = {
    "서울특별시": ("서울", "1100000"),
    "부산광역시": ("부산", "2600000"),
    "대구광역시": ("대구", "2700000"),
    "인천광역시": ("인천", "2800000"),
    "광주광역시": ("광주", "2900000"),
    "대전광역시": ("대전", "3000000"),
    "울산광역시": ("울산", "3100000"),
    "세종특별자치시": ("세종", "3200000"),
    "경기도": ("경기", "4100000"),
    "강원도": ("강원", "4200000"),
    "강원특별자치도": ("강원", "4200000"),
    "충청북도": ("충북", "4300000"),
    "충청남도": ("충남", "4400000"),
    "전라북도": ("전북", "4500000"),
    "전북특별자치도": ("전북", "4500000"),
    "전라남도": ("전남", "4600000"),
    "경상북도": ("경북", "4700000"),
    "경상남도": ("경남", "4800000"),
    "제주특별자치도": ("제주", "4900000"),
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


def fetch_page(key: str, year: int, page: int) -> str:
    params = {
        "Key": key,
        "Type": "xml",
        "pIndex": page,
        "pSize": PAGE_SIZE,
        "fyr": str(year),
    }
    try:
        resp = requests.get(ENDPOINT, params=params, timeout=60)
    except requests.RequestException as exc:
        raise ApiError(f"{year} p{page} 네트워크 오류: {exc}") from exc
    if resp.status_code != 200:
        raise ApiError(f"{year} p{page} HTTP {resp.status_code}")
    return resp.text


def collect_one(key: str, year: int) -> str:
    main_path = RAW_DIR / f"{ENDPOINT_NAME}_{year}.xml"
    if main_path.exists():
        return "skipped"

    body = fetch_page(key, year, 1)
    code, message = parse_result_code(body)
    if code == NO_DATA_CODE:
        main_path.write_text(body, encoding="utf-8")
        return "nodata"
    if code != SUCCESS_CODE:
        raise ApiError(
            f"{year} 예상치 못한 응답코드 {code!r} ({message!r})\n"
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
        extra = fetch_page(key, year, page)
        ecode, emsg = parse_result_code(extra)
        if ecode != SUCCESS_CODE:
            raise ApiError(f"{year} p{page} 응답코드 {ecode!r} ({emsg!r})")
        (RAW_DIR / f"{ENDPOINT_NAME}_{year}_p{page}.xml").write_text(
            extra, encoding="utf-8"
        )
        n = len(ET.fromstring(extra).findall("row"))
        if n == 0:
            raise ApiError(f"{year} p{page} 행 0개인데 {got}/{total}")
        got += n

    return "fetched"


def build_csv() -> tuple[list[dict], dict]:
    rows = []
    observed_codes: dict[str, set] = {}
    unmapped = set()

    for path in sorted(RAW_DIR.glob(f"{ENDPOINT_NAME}_*.xml")):
        root = ET.fromstring(path.read_text(encoding="utf-8"))
        for row in root.findall("row"):
            full = row.findtext("wa_laf_hg_nm")
            if full not in SIDO_MAP:
                unmapped.add(full)
                continue
            short, _ = SIDO_MAP[full]
            observed_codes.setdefault(short, set()).add(row.findtext("wa_laf_cd"))
            rows.append(
                {
                    "회계연도": row.findtext("fyr"),
                    "시도명": short,
                    "계층": row.findtext("cap_dv_nm"),
                    "세목명": row.findtext("dtmk_nm"),
                    "금액": int(row.findtext("rcvmt_aggr_amt") or 0),
                    "비중": row.findtext("rate"),
                }
            )

    if unmapped:
        raise SystemExit(f"[중단] SIDO_MAP에 없는 시도명: {sorted(unmapped)}")

    rows.sort(key=lambda r: (r["회계연도"], r["시도명"], r["계층"], r["세목명"]))

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(
            fh, fieldnames=["회계연도", "시도명", "계층", "세목명", "금액", "비중"]
        )
        w.writeheader()
        w.writerows(rows)

    return rows, observed_codes


def main() -> int:
    key = load_key()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    stats = {"fetched": 0, "skipped": 0, "nodata": 0}
    nodata = []
    for year in YEARS:
        try:
            status = collect_one(key, year)
        except ApiError as exc:
            print(f"\n[중단] {exc}", file=sys.stderr)
            return 1
        stats[status] += 1
        if status == "nodata":
            nodata.append(year)
        if status == "fetched":
            time.sleep(SLEEP_SEC)
        print(f"  {year}: {status}")

    print(f"\n[수집] {stats}")
    if nodata:
        print(f"[데이터 없음] {nodata}")

    rows, observed = build_csv()
    print(f"[CSV] {CSV_PATH} — {len(rows)}행 (전 계층: 시도/시/군)")
    print("[코드 매핑] KAAAE 2자리 코드 <-> 약칭:")
    for short in sorted(observed):
        print(f"    {short}: {sorted(observed[short])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
