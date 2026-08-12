"""예산대비채무비율 OpenAPI 단일 호출 테스트.

행정안전부 지방재정365 (공공데이터포털 15057548, API 유형 LINK).
엔드포인트/파라미터명은 지방재정365 OpenAPI 명세에서 확인한 값이다.

호출은 1건만. 반복 호출 금지.
"""

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

# 요청주소 (지방재정365 OpenAPI 명세)
ENDPOINT = "https://www.lofin365.go.kr/lf/hub/HEDFC"

# 경기 지역코드 — 지방재정365 Sheet 화면에서 확인 (경기본청 자치단체코드와 동일)
GYEONGGI_REGION_CODE = "4100000"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = PROJECT_ROOT / "data" / "raw" / "sample_response.xml"

# 공공데이터포털이 인증 실패 시 XML로 돌려주는 resultCode들
AUTH_ERROR_TOKENS = (
    "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
    "SERVICE_ACCESS_DENIED_ERROR",
    "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR",
    "APPLICATION_ERROR",
    "UNKNOWN_ERROR",
    "HTTP_ERROR",
    "INVALID_REQUEST_PARAMETER_ERROR",
    "NO_OPENAPI_SERVICE_ERROR",
)

# 지방재정365(lofin) 자체 응답코드. INFO-000이 정상, 그 외는 확인 필요.
LOFIN_SUCCESS_CODE = "INFO-000"


def mask(secret: str) -> str:
    """인증키를 로그에 안전하게 표시."""
    if not secret:
        return "(없음)"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-4:]} (len={len(secret)})"


def masked_url(url: str, params: dict, key: str) -> str:
    """최종 요청 URL에서 인증키만 가린 문자열."""
    safe = dict(params)
    if "Key" in safe:
        safe["Key"] = "***MASKED***"
    return f"{url}?{urlencode(safe)}"


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    service_key = os.getenv("DATA_GO_KR_KEY", "").strip()

    params = {
        "Type": "xml",
        "pIndex": 1,
        "pSize": 1,          # 1건만
        "fyr": "2024",       # 회계연도
        "wa_laf_cd": GYEONGGI_REGION_CODE,  # 지역코드(경기)
    }

    if service_key:
        # dict 순서상 Key를 앞에 두기 위해 재구성
        params = {"Key": service_key, **params}
        print(f"[인증키] .env DATA_GO_KR_KEY 사용: {mask(service_key)}")
    else:
        print(
            "[경고] .env에 DATA_GO_KR_KEY가 비어 있음. "
            "Key 파라미터 없이 호출한다 (명세상 sample key로 동작, pIndex=1 / pSize=5 고정)."
        )

    print(f"[요청 URL] {masked_url(ENDPOINT, params, service_key)}")

    try:
        resp = requests.get(ENDPOINT, params=params, timeout=30)
    except requests.RequestException as exc:
        print(f"[실패] 요청 예외: {exc}", file=sys.stderr)
        return 1

    print(f"[상태코드] {resp.status_code}")
    print(f"[Content-Type] {resp.headers.get('Content-Type', '(없음)')}")

    body = resp.text

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(body, encoding="utf-8")
    print(f"[저장] {OUTPUT_PATH}  ({len(body.encode('utf-8'))} bytes)")

    print("\n----- 응답 원문 (가공 없음) -----")
    print(body)
    print("----- 응답 원문 끝 -----\n")

    # 에러 판별
    upper = body.upper()
    hits = [t for t in AUTH_ERROR_TOKENS if t in upper]
    if hits:
        print(f"[판정] 공공데이터포털 에러코드 감지: {', '.join(hits)}")
        return 2

    code_match = re.search(r"<CODE>\s*([^<\s]+)\s*</CODE>", body, re.IGNORECASE)
    if code_match:
        code = code_match.group(1)
        if code == LOFIN_SUCCESS_CODE:
            print(f"[판정] lofin 응답코드 {code} — 정상 처리.")
        else:
            print(f"[판정] lofin 응답코드 {code} — 정상(INFO-000)이 아님. 원문 확인 필요.")
            return 2

    if "<row>" in body.lower() or "<rate>" in body.lower():
        print("[판정] 실제 데이터 행이 응답에 포함됨.")
        return 0

    print("[판정] 데이터 행을 찾지 못함. 위 원문 확인 필요.")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
