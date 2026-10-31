"""
회사 노드 정제·통합 모듈 (2026 미래에셋 AI Festival 금융상품 Agent)

목적:
  ETF 구성종목명(주식)과 채권 발행사명을 하나의 fin:Company 노드로 통합.
  멀티홉 질문("삼성전자 담은 ETF ↔ 삼성전자 발행 채권")을 위한 엔티티 정합.

핵심 결정(데이터 실태 기반):
  - 티커가 신뢰 가능한 국내 상장주식(6자리 유효코드)만 회사 노드로 통합
  - 해외종목은 티커가 심하게 오염(임시코드 재활용)되어 이번 단계 제외
  - 우선주(17개)는 보통주 회사로 통합, 원본 코드는 속성 보존
  - 채권 발행사는 채권명에서 파싱 후 국내주식과 정확매칭되면 연결,
    안되면(캐피탈/카드/발전 등 비상장) 발행사 노드로만 존재

출력:
  회사 노드 딕셔너리 + ETF보유관계 + 채권발행관계 (TTL 생성 전 중간산출물)
"""

import re
import openpyxl
from collections import defaultdict, Counter


# =============================================================
# 1. 정규화 유틸
# =============================================================


def _col_index(header, name):
    """컬럼명을 대소문자 무관하게 찾는다. (새 데이터 소문자화 대응)"""
    low = [str(h).lower() for h in header]
    return low.index(name.lower())


# 직접 스크립트 실행과 python -m 실행을 모두 지원한다.
if __package__:
    from .company_identity import (
        normalize_name, normalize_for_match, CompanyRegistry,
        build_company_match_index,
    )
else:
    from company_identity import (
        normalize_name, normalize_for_match, CompanyRegistry,
        build_company_match_index,
    )


# 우선주 접미사 패턴 (종목명 끝)
PREF_SUFFIX = re.compile(r"(우[A-C]?$|\d우[A-C]?$)")


def strip_pref_suffix(name: str) -> str:
    """우선주 종목명 -> 보통주명 추정. '현대차2우B'->'현대차', '삼성전자우'->'삼성전자'"""
    return re.sub(r"\d?우[A-C]?$", "", name)


# 비종목 항목 필터 (현금성·설정·선물 등). 6자리 유효코드가 아니면 대부분 걸러지지만 보강용.
NON_STOCK_KEYWORDS = ["현금", "예금", "설정현금", "설정", "선물", "FUT", "국고채", "통안", "(CP)", "원화"]

# ETF 브랜드 접두 (재간접 구조로 ETF가 다른 ETF를 구성종목에 담은 경우 제외)
# 주의: 회사명과 충돌하는 브랜드(BNK금융지주 등)는 제외. ETF는 보통 '브랜드 + 지수/테마'.
ETF_BRANDS = ["KODEX", "TIGER", "RISE", "KBSTAR", "KIWOOM",
              "HANARO", "SOL", "TIMEFOLIO", "ARIRANG",
              "KOSEF", "히어로즈", "FOCUS", "TREX",
              "KoAct", "ACE 미국", "ACE 국고", "ACE 종합", "ETN"]

# 공채/특수채 키워드 (회사가 아닌 채권)
PUBLIC_BOND_KEYWORDS = ["토지주택채권", "도로공사", "고속도로건설채권", "지역개발채권",
                        "도시철도채권", "국민주택", "MBS", "전력채", "수자원"]

# 국내 상장사 영문약칭 화이트리스트 (한글이 없어 해외로 오분류되는 것 방지)
DOMESTIC_ALPHA = {
    "SK", "KT", "LG", "GS", "CJ", "DL", "DB", "LS", "HL", "HD현대", "LX",
    "NAVER", "POSCO", "KT&G", "KCC", "S-OIL", "S-OIL", "HDC", "OCI", "BGF",
    "SKC", "HMM", "SBS", "STX", "KG", "SNT", "NHN", "KG모빌리티",
    "CJ ENM", "CJ_ENM", "LS ELECTRIC", "LS_ELECTRIC", "INVENI",
}


def has_hangul(s: str) -> bool:
    """한글 포함 여부."""
    return bool(re.search(r"[가-힣]", s or ""))


def is_domestic_company(company_key: str, info: dict) -> bool:
    """
    국내 상장사 판정. 다음 중 하나라도 만족하면 국내로 본다.
      1) 회사명에 한글 포함
      2) 채권 발행사와 연결됨(국내채권 마스터이므로 발행사는 국내 확실)
      3) 국내 영문약칭 화이트리스트에 있음
    해외종목은 티커가 오염(임시코드 재활용)되어 이번 단계에서 제외한다.
    """
    if has_hangul(company_key):
        return True
    if info.get("bond_issuer_names"):
        return True
    key_upper = company_key.upper().replace(" ", "")
    if key_upper in {a.upper().replace(" ", "") for a in DOMESTIC_ALPHA}:
        return True
    return False


def is_non_stock(name: str, ticker: str) -> bool:
    """비종목(현금성 등) 여부. 국내주식 회사노드에서 제외 대상."""
    if any(k in name for k in NON_STOCK_KEYWORDS):
        return True
    return False


# 채권 회차/특약 패턴 (ETF 구성종목에 섞인 개별 채권 판별용)
#   예: '삼성카드2517', '신한은행28-09-이-2-C', '우리카드265-2(사)', 'LG에너지솔루션3-2(녹)'
BOND_SUFFIX = re.compile(
    r"\d+-\d+"                          # 하이픈 회차: 265-2, 28-09
    r"|(이표|이-|\d갑|\d을|-이-|이\d갑)"   # 이표/갑/을 채권표기
    r"|\d+\s*\d+/\d+"                    # 표면금리: 4 1/4
    r"|\d{2,}(\(.*\))?$"                # 끝 회차숫자(+특약): 2517, 569-1(녹)
)


def is_bond_name(name: str, bond_name_set: set) -> bool:
    """
    ETF 구성종목명이 개별 채권/상품/공채인지 판별(회사 노드에서 제외 대상).
    1) 채권 마스터에 정확히 있으면 채권 확정
    2) 채권 회차/특약 패턴
    3) ETF 브랜드(재간접 ETF), 공채, 회차 잔여물
    순수 회사명(카페24, PHILLIPS 66)은 살림.
    """
    if name in bond_name_set:
        return True
    # 특약기호(하이픈회차/이표/갑을/녹사 등)
    if re.search(r"\d+-\d+|이표|\d갑|\d을|-이-|\(사\)|\(녹\)|\(변\)|\(후\)", name):
        return True
    # ETF 브랜드로 시작(재간접 ETF가 구성종목에 섞임)
    for brand in ETF_BRANDS:
        if brand in name:
            return True
    # 공채/특수채
    if any(kw in name for kw in PUBLIC_BOND_KEYWORDS):
        return True
    # ETF/파생 잔여 패턴 (합성복제·MSCI지수·전단채·조건부자본증권·현물 등)
    #   주의: '동남합성' 같은 회사명 오탐 방지 위해 '합성 H'/'합성H'(ETF표기)만 제외
    if re.search(r"합성\s?H|MSCI|전단채|조건부\(|조건부자본|현물\s?\d|플러스액티브|액티브$", name):
        return True
    # 회차 잔여물: 금융계열 명칭 + 끝 숫자 (지주185, 카드671, 캐피탈108)
    if re.search(r"(지주|카드|캐피탈|증권|은행|보험|금융)\d+$", name):
        return True
    return False


def load_bond_names(bond_master_path: str) -> set:
    """채권 마스터의 상품명 집합 로드 (구성종목 채권 판별용)."""
    wb = openpyxl.load_workbook(bond_master_path, read_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = list(next(rows))
    i_nm = _col_index(header, "PD_NM")
    names = set()
    for r in rows:
        if r[i_nm]:
            names.add(str(r[i_nm]).strip())
    wb.close()
    return names


# =============================================================
# 2. 국내 상장주식 회사 노드 구축 (ETF 구성종목 출처)
# =============================================================

def build_stock_companies(etf_portfolio_path: str, bond_name_set: set = None):
    """
    ETF 구성종목에서 국내 상장주식 회사 노드 구축.
    bond_name_set: 채권 마스터 상품명 집합(개별 채권 구성종목 제외용). None이면 스킵.
    반환:
      companies: {회사키: {'name':대표명, 'codes':set(코드), 'norm':정규화키}}
      code_to_company: {종목코드: 회사키}
    회사키는 대표 보통주명(우선주는 보통주로 통합됨).
    """
    if bond_name_set is None:
        bond_name_set = set()
    wb = openpyxl.load_workbook(etf_portfolio_path, read_only=True)

    # 코드별 종목명 빈도 집계
    code_names = defaultdict(Counter)
    for sh in wb.sheetnames:
        ws = wb[sh]
        rows = ws.iter_rows(values_only=True)
        next(rows, None)  # 헤더 스킵
        for r in rows:
            if len(r) < 3:
                continue
            ticker = str(r[1]).strip() if r[1] is not None else ""
            name = str(r[2]).strip() if r[2] is not None else ""
            if not name:
                continue
            # 국내 상장주식만: 6자리 숫자 코드
            if len(ticker) == 6 and ticker.isdigit():
                if is_non_stock(name, ticker):
                    continue
                # 개별 채권이 구성종목으로 섞인 것 제외 (채권형 ETF 보유분)
                if is_bond_name(name, bond_name_set):
                    continue
                code_names[ticker][name] += 1
    wb.close()

    # 코드별 대표 종목명(최빈)
    code_repr = {c: names.most_common(1)[0][0] for c, names in code_names.items()}

    # 우선주 -> 보통주명 매핑
    # 회사키 = 보통주명. 우선주는 접미사 제거한 보통주명으로 흡수.
    all_names = set(code_repr.values())
    companies = {}          # 회사키 -> 정보
    code_to_company = {}    # 코드 -> 회사키

    for code, name in code_repr.items():
        if PREF_SUFFIX.search(name):
            base = strip_pref_suffix(name)
            # 보통주가 실제 존재하면 그쪽으로 통합, 아니면 자기 자신을 키로
            company_key = base if base in all_names else name
        else:
            company_key = name

        if company_key not in companies:
            companies[company_key] = {
                "name": company_key,
                "codes": set(),
                "norm": normalize_for_match(company_key),
                "aliases": set(),
            }
        companies[company_key]["codes"].add(code)
        if name != company_key:
            companies[company_key]["aliases"].add(name)  # 우선주명 등 별칭 보존
        code_to_company[code] = company_key

    # 명시적 별칭만 대표 회사로 합친다. 미등록 기존 회사의 ID는 유지한다.
    registry = CompanyRegistry()
    canonical = {}
    remap = {}
    for key, info in companies.items():
        company_id, name = registry.identity(key, existing=True)
        remap[key] = name
        entry = canonical.setdefault(name, {
            "id": company_id, "name": name, "norm": registry.match_key(name),
            "codes": set(), "aliases": set(),
        })
        entry["codes"].update(info["codes"])
        entry["aliases"].update(info["aliases"])
        if key != name:
            entry["aliases"].add(key)
    build_company_match_index(canonical, registry)
    return canonical, {code: remap[key] for code, key in code_to_company.items()}


# =============================================================
# 3. 채권 발행사 파싱 + 회사 매칭
# =============================================================

def parse_issuer(bond_name: str) -> str:
    """채권명에서 발행사명 추출. 뒤쪽 회차·특약 제거."""
    s = bond_name.strip()
    # 회사명 = 첫 숫자 앞까지 (한글/영문/공백)
    m = re.match(r"^([가-힣A-Za-z]+(?:\s?[가-힣A-Za-z]+)*?)\s?\d", s)
    if m:
        return m.group(1).strip()
    # 숫자 없으면 괄호 앞까지
    m = re.match(r"^([가-힣A-Za-z]+(?:\s?[가-힣A-Za-z]+)*?)\s?\(", s)
    if m:
        return m.group(1).strip()
    return s


def iter_bond_issuers(bond_master_path):
    """모든 생성 경로의 발행사 추출: PD_PBCM 우선, 빈 값만 상품명 파싱."""
    wb = openpyxl.load_workbook(bond_master_path, read_only=True)
    try:
        rows = wb.active.iter_rows(values_only=True)
        header = list(next(rows))
        i_no = _col_index(header, "PD_NO")
        i_nm = _col_index(header, "PD_NM")
        i_pbcm = _col_index(header, "PD_PBCM")
        for row in rows:
            bond_no = str(row[i_no]).strip() if row[i_no] is not None else ""
            pbcm = row[i_pbcm]
            issuer = str(pbcm).strip() if pbcm is not None else ""
            if not issuer:
                issuer = parse_issuer(str(row[i_nm] or ""))
            yield bond_no, issuer
    finally:
        wb.close()


def match_bond_issuers(bond_master_path: str, companies: dict):
    """
    채권 발행사를 PD_PBCM(발행사 컬럼)에서 직접 취득해 국내주식 회사와 매칭.
    PD_PBCM은 현재 법인명이 정제되어 들어있어(97.8% 채움), PD_NM 파싱보다
    사명변경·회차·특약 노이즈가 적다. 비어있는 소수 건만 PD_NM 파싱으로 폴백.
    반환:
      issuer_to_company: {발행사명: 회사키}   # 매칭 성공(국내주식과 동일)
      standalone_issuers: {발행사명: 채권수}   # 매칭 실패(발행사 노드로만 존재)
    """
    # 정규화키 -> 회사키 역인덱스 (브랜드 접두 통일 적용: 한글발음<->영문브랜드 매칭)
    registry = CompanyRegistry()
    norm_to_company = build_company_match_index(companies, registry)

    issuer_count = Counter(issuer for _, issuer in iter_bond_issuers(bond_master_path)
                           if issuer)

    issuer_to_company = {}
    standalone_issuers = {}
    for issuer, cnt in issuer_count.items():
        key = norm_to_company.get(registry.match_key(issuer))
        if key:
            issuer_to_company[issuer] = key
        else:
            standalone_issuers[issuer] = cnt

    return issuer_to_company, standalone_issuers


# =============================================================
# 4. 실행 및 리포트
# =============================================================

if __name__ == "__main__":
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[2]  # src/graph/resolve_company.py -> fin-013/
    RAW_DIR = ROOT / "data" / "raw"

    def _find(code_or_name):
        exact = RAW_DIR / code_or_name
        if exact.exists():
            return str(exact)
        cands = sorted(RAW_DIR.glob(f"*{code_or_name}*.xlsx"))
        if cands:
            return str(cands[0])
        raise FileNotFoundError(f"'{code_or_name}' 파일을 {RAW_DIR}에서 찾지 못했습니다.")

    PORTFOLIO = _find("etf_portfolio.xlsx")
    BOND = _find("PRBD01N001_국내채권마스터")

    print("[1] 국내주식 회사 노드 구축 중...")
    companies, code_to_company = build_stock_companies(PORTFOLIO)
    print(f"    회사 노드: {len(companies)}개")
    print(f"    매핑된 종목코드: {len(code_to_company)}개")

    # 우선주 통합 확인
    merged = {k: v for k, v in companies.items() if v["aliases"]}
    print(f"    우선주 통합된 회사: {len(merged)}개")
    for k, v in list(merged.items())[:5]:
        print(f"      {k}: codes={sorted(v['codes'])} aliases={v['aliases']}")

    print("\n[2] 채권 발행사 매칭 중...")
    issuer_to_company, standalone = match_bond_issuers(BOND, companies)
    print(f"    국내주식과 매칭된 발행사: {len(issuer_to_company)}개 (멀티홉 연결점)")
    print(f"    독립 발행사(비상장 등): {len(standalone)}개")

    print("\n[3] 멀티홉 연결 예시(상위):")
    # 매칭된 발행사를 채권수로 정렬하려면 count 재계산 필요 - 여기선 회사키 기준 예시
    for issuer, key in list(issuer_to_company.items())[:10]:
        codes = sorted(companies[key]["codes"])
        print(f"    채권발행사 '{issuer}' <-> 주식 '{key}' (코드 {codes})")