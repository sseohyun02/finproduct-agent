"""
테마 노드 추출 모듈 (2026 미래에셋 AI Festival)

목적:
  상품명에서 산업 투자테마(반도체·AI·방산 등)를 추출해 Theme 노드로 만들고,
  상품 --hasTheme--> 테마 관계 생성. "반도체 ETF들 공통보유 종목" 같은 교차질의 지원.

결정사항:
  - 산업 테마만 채택. 배당·ESG·리츠·인컴 등 '투자 스타일'은 제외(SQL 속성 후보).
  - 상품구조 노이즈(증권상장지수투자신탁 등)를 먼저 제거 후 테마 탐색(오검출 방지).
  - 상품 URI는 회사노드(build_ttl.py)와 정합:
      국내ETF: pd_itm_no_ma의 접두문자 제거→6자리 = ex:Product_{6자리}
      해외ETF: pd_itm_no,  펀드: itm_no 그대로 (구성종목 없어 회사노드와 안겹침)
"""

import re
import openpyxl


# =============================================================
# 산업 테마 사전 (키워드 -> 테마명)
# 각 테마는 대표 키워드 목록. '전지' 단독 등 오검출 유발 키워드는 배제.
# =============================================================
THEME_DICT = {
    "반도체": ["반도체", "비메모리", "파운드리", "소부장"],
    "2차전지": ["2차전지", "이차전지", "배터리"],
    "AI": ["AI", "인공지능"],
    "바이오헬스케어": ["바이오", "헬스케어", "제약"],
    "방산": ["방산", "국방", "우주항공"],
    "로봇": ["로봇"],
    "자율주행": ["자율주행"],
    "전기차": ["전기차"],
    "모빌리티": ["모빌리티"],
    "클린에너지": ["클린에너지", "신재생", "태양광", "풍력", "수소"],
    "메타버스": ["메타버스"],
    "콘텐츠엔터": ["K콘텐츠", "K-콘텐츠", "엔터", "게임"],
    "금융섹터": ["은행주", "증권주", "보험주"],
    "조선해운": ["조선", "해운"],
    "원자력": ["원자력", "SMR"],
    "탄소배출권": ["탄소배출권"],
}

# 상품구조 노이즈 (테마 탐색 전에 상품명에서 제거)
STRUCT_NOISE = [
    "증권상장지수투자신탁", "상장지수투자신탁", "증권자투자신탁", "증권투자신탁",
    "파생결합증권", "상장지수증권", "자투자신탁", "투자신탁", "재간접형",
    "파생형", "주식혼합", "채권혼합", "혼합자산", "상장지수", "증권",
]

# AI 오검출 방지: AI가 다른 단어의 일부인 경우 제외할 패턴
#   진짜 AI 테마는 'AI테크','글로벌AI','AI&' 등. 단독/구분자 인접일 때만 인정.
AI_VALID = re.compile(r"(^|[^A-Za-z])AI([^A-Za-z]|$)")


def clean_struct(name: str) -> str:
    """상품명에서 구조 노이즈 제거."""
    s = name
    for w in STRUCT_NOISE:
        s = s.replace(w, "")
    return s


def extract_themes(product_name: str) -> list:
    """상품명에서 산업 테마 추출. 여러 테마 가능."""
    if not product_name:
        return []
    cleaned = clean_struct(product_name)
    found = []
    for theme, kws in THEME_DICT.items():
        matched = False
        for kw in kws:
            if kw == "AI":
                # AI는 단어경계 검사(원문 기준, 대소문자 구분 유지)
                if AI_VALID.search(product_name):
                    matched = True
                    break
            elif kw in cleaned:
                matched = True
                break
        if matched:
            found.append(theme)
    return found


# =============================================================
# 상품 URI 매핑 (회사노드와 정합)
# =============================================================

def product_uri_domestic_etf(pd_itm_no_ma: str) -> str:
    """국내ETF: 'A305080' -> ex:Product_305080 (회사노드 holds와 동일 키)."""
    code = re.sub(r"^[A-Za-z]", "", str(pd_itm_no_ma).strip())
    return f"ex:Product_{code}"


def product_uri_generic(code: str) -> str:
    """해외ETF·펀드: 코드 그대로(특수문자만 치환)."""
    c = re.sub(r"[\s&().,/'\"]+", "_", str(code).strip())
    return f"ex:Product_{re.sub(r'_+', '_', c).strip('_')}"


# =============================================================
# 실행: 4종 상품에서 테마 추출 -> TTL
# =============================================================

def build_theme_ttl():
    theme_set = set()          # 등장한 테마명
    has_theme_lines = []       # 상품 --hasTheme--> 테마

    sources = [
        ("PREF01N001_국내ETF마스터_20260711_datarows.xlsx", "pd_nm", "pd_itm_no_ma", "detf"),
        ("PREF02N001_해외ETF마스터_20260711_datarows.xlsx", "pd_nm", "pd_itm_no", "oetf"),
        ("PRFD01N001_공모펀드마스터_20260711_datarows.xlsx", "itm_nm", "itm_no", "fund"),
    ]

    stats = {}
    for path, nmcol, codecol, kind in sources:
        wb = openpyxl.load_workbook(path, read_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        header = list(next(rows))
        i_nm = header.index(nmcol)
        i_code = header.index(codecol)
        n_prod = 0
        n_rel = 0
        for r in rows:
            name = (r[i_nm] or "").strip()
            code = (r[i_code] or "")
            if not name or not code:
                continue
            themes = extract_themes(name)
            if not themes:
                continue
            if kind == "detf":
                puri = product_uri_domestic_etf(code)
            else:
                puri = product_uri_generic(code)
            for th in themes:
                theme_set.add(th)
                turi = f"ex:Theme_{th}"
                has_theme_lines.append(f"{puri} fin:hasTheme {turi} .")
                n_rel += 1
            n_prod += 1
        wb.close()
        stats[kind] = {"themed_products": n_prod, "relations": n_rel}

    # 테마 노드 정의
    theme_node_lines = []
    for th in sorted(theme_set):
        theme_node_lines.append(
            f'ex:Theme_{th} a fin:Theme ;\n    rdfs:label "{th}"@ko .'
        )

    return theme_node_lines, has_theme_lines, sorted(theme_set), stats


if __name__ == "__main__":
    theme_nodes, has_theme, themes, stats = build_theme_ttl()

    FIN = "http://mirae-agent.example/ontology#"
    EX = "http://mirae-agent.example/data#"
    header = f"""@prefix fin: <{FIN}> .
@prefix ex:  <{EX}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

#################################################################
#  인스턴스: 테마 노드 + 상품-테마 관계(hasTheme)
#################################################################
"""
    with open("/home/claude/out/instances_theme.ttl", "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n### 테마 노드 ###\n")
        f.write("\n".join(theme_nodes))
        f.write("\n\n### 상품-테마 관계 ###\n")
        f.write("\n".join(has_theme))
        f.write("\n")

    print("=== 테마 추출 결과 ===")
    print(f"테마 종류: {len(themes)}개 -> {themes}")
    print(f"hasTheme 관계: {len(has_theme)}개")
    print()
    for k, v in stats.items():
        print(f"  {k}: 테마상품 {v['themed_products']}개, 관계 {v['relations']}개")