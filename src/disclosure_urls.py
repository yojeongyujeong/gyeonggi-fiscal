"""도별 재정공시(결산) 연도별 페이지 URL 매핑.

**회계연도 기준으로 적는다.** 공시연도 = 회계연도 + 1 인 곳이 대부분이므로
사이트에서 보이는 연도 표기와 다를 수 있다. 예: 경북 mnu_uid=14981 은
사이트상 '2025년'이지만 내용은 2024 회계연도 결산이다.

FINLK가 주는 링크는 진입점 힌트로만 쓴다. 경북은 URL 자체가 무효였고
(mnu_uid=2683 -> "정상적인 메뉴접근이 아닙니다"), 4개 시도 모두 결산이 아니라
예산 공시로 연결됐다. 아래는 사이트에서 직접 찾은 결산 페이지다.

PAGES[시도][회계연도] = 결산 공시 페이지 URL
"""

PAGES: dict[str, dict[int, str]] = {
    # /balanceSheet/{공시연도}
    "강원": {
        fy: f"https://state.gwd.go.kr/portal/administration/finance/financialReporting/balanceSheet/{fy+1}"
        for fy in (2021, 2022, 2023, 2024)
    },
    # 연도 <select> 의 option value (menuNo)
    "충남": {
        2021: "https://www.chungnam.go.kr/finance/main/contents.do?menuNo=2300071",
        2022: "https://www.chungnam.go.kr/finance/main/contents.do?menuNo=2300069",
        2023: "https://www.chungnam.go.kr/finance/main/contents.do?menuNo=2300146",
        2024: "https://www.chungnam.go.kr/finance/main/contents.do?menuNo=2300153",
    },
    # 연도 링크 mnu_uid. 사이트 표기 연도 = 회계연도+1
    "경북": {
        2021: "https://www.gb.go.kr/Main/finace/page.do?mnu_uid=14940&mnu_order=3",
        2022: "https://www.gb.go.kr/Main/finace/page.do?mnu_uid=14792&mnu_order=3",
        2023: "https://www.gb.go.kr/Main/finace/page.do?mnu_uid=14871&mnu_order=3",
        2024: "https://www.gb.go.kr/Main/finace/page.do?mnu_uid=14981&mnu_order=3",
    },
}

# 사용자가 직접 확인해 제공한 FY2024 결산 공시 페이지.
# 2021~2023은 구형 .hwp 벽 때문에 포기했고 FY2024 단년도만 수집한다.
PAGES["충북"] = {2024: "https://www.chungbuk.go.kr/finance/contents.do?key=5341"}
PAGES["전남"] = {2024: "https://www.jeonnam.go.kr/contentsView.do?menuId=jeonnam0305011900"}
# 사용자가 준 DOM_000000138003007000 은 '결산서' 페이지라 기금표가 없다.
# 재정공시 > 결산기준 은 DOM_000000138003008002 이다.
PAGES["경남"] = {2024: "https://www.gyeongnam.go.kr/index.gyeong?menuCd=DOM_000000138003008002"}

# 페이지에서 링크를 긁을 수 없는 경우의 직접 파일 URL (우선순위 순).
# 경남은 href가 javascript:download(경로, 파일명) 이라 정규식으로 못 잡는다.
# 실제 URL 규칙: /mng/file/dolawnod.gyeong?thap=open/budget{경로}&mnelif={파일명}
FILES: dict[str, dict[int, list[str]]] = {
    "경남": {
        2024: [
            "https://www.gyeongnam.go.kr/mng/file/dolawnod.gyeong"
            "?thap=open/budget/finance/gongsi/gongsi2025_f/&mnelif=gongsi2025_all2.hwpx",
            "https://www.gyeongnam.go.kr/mng/file/dolawnod.gyeong"
            "?thap=open/budget/finance/gongsi/gongsi2025_f/&mnelif=gongsi2025_all2.pdf",
        ]
    },
}

# 결산 페이지 URL을 확보하지 못한 시도.
UNRESOLVED: dict[str, str] = {
    "전북": "https://www.jeonbuk.go.kr/index.jeonbuk?menuCd=DOM_000000103001006000",
}
