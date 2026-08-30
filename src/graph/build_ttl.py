"""
TTL 인스턴스 생성 (2026 미래에셋 AI Festival)

입력(정제 산출물):
  company_nodes.json    : 회사 노드 (국내 상장주식 1,910개)
  code_to_company.json  : 종목코드 -> 회사키

출력:
  instances_company.ttl : 회사 노드 + ETF보유내역(Holding, 비중) + 채권발행관계

노드/관계 구조:
  fin:Company           회사 (국내 상장주식)
  fin:Holding           보유내역 노드 (ETF가 회사를 몇% 담았는지)
  ETF --hasHolding--> Holding --ofCompany--> Company, Holding에 weight(비중)
  ETF --holds--> Company (비중없는 지름길, 단순질문용)
  Bond --issuedBy--> Company (채권 발행)

주의:
  - 회사키(한글명)를 URI로 쓸 때 안전하게 인코딩
  - 비중이 0이거나 없는 보유행은 Holding 생성 제외(현금성/오류 방지)
"""

import json
import re
import glob
from pathlib import Path
import openpyxl
from urllib.parse import quote

FIN = "http://mirae-agent.example/ontology#"
EX = "http://mirae-agent.example/data#"  # 인스턴스 네임스페이스

# 프로젝트 루트: 이 파일이 src/graph/build_ttl.py에 있다는 전제로 2단계 상위.
# (fin-013/src/graph/build_ttl.py -> fin-013/)
ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
GRAPH_DIR = ROOT / "data" / "graph"


def find_raw_file(code_or_name: str) -> str:
    """
    data/raw/에서 파일을 찾는다. 프로젝트 관례(load_excel.py)와 동일하게
    '테이블코드(PRBD01N001 등)를 파일명에 포함하는 첫 매치'를 사용해
    실제 파일명이 조금 달라도(예: 날짜 접미사 차이) 안전하게 찾는다.
    확장자가 이미 붙은 정확한 파일명(etf_portfolio.xlsx 등)이면 그대로 사용.
    """
    exact = RAW_DIR / code_or_name
    if exact.exists():
        return str(exact)
    candidates = sorted(RAW_DIR.glob(f"*{code_or_name}*.xlsx"))
    if candidates:
        return str(candidates[0])
    raise FileNotFoundError(
        f"'{code_or_name}' 관련 파일을 {RAW_DIR}에서 찾지 못했습니다. "
        f"data/raw/ 폴더에 원본 엑셀이 있는지 확인하세요."
    )


def safe_local(s: str) -> str:
    """문자열을 URI 지역명으로 변환. 한글 유지, 특수문자만 언더스코어 치환."""
    s = str(s).strip()
    s = re.sub(r"[\s&().,/'\"]+", "_", s)   # 공백·특수문자 -> _
    s = re.sub(r"_+", "_", s).strip("_")     # 연속 _ 정리
    return s


def company_uri(company_key: str) -> str:
    return f"ex:Company_{safe_local(company_key)}"


def load_inputs():
    # JSON 덤프 대신 resolve_company에서 직접 회사 노드를 구축(패치 일관성 보장).
    # 원본 그래프(국내상장 1,335개) 재현을 위해 다음 순서를 지킨다:
    #   1) ETF 구성종목에서 회사 후보 구축(6자리코드)
    #   2) 채권 발행사 매칭으로 bond_issuer_names 주입(국내 판정 근거)
    #   3) is_domestic_company로 해외종목 제외(티커 오염분 배제)
    from resolve_company import (
        load_bond_names, build_stock_companies,
        match_bond_issuers, is_domestic_company,
    )
    bond_path = find_raw_file("PRBD01N001_국내채권마스터")
    portfolio_path = find_raw_file("etf_portfolio.xlsx")

    bond_names = load_bond_names(bond_path)
    companies, code_to_company = build_stock_companies(portfolio_path, bond_names)

    # 채권 발행사 매칭 -> 매칭된 회사에 발행사명 기록(국내 판정 근거)
    issuer_to_company, _ = match_bond_issuers(bond_path, companies)
    for issuer, ckey in issuer_to_company.items():
        companies[ckey].setdefault("bond_issuer_names", set()).add(issuer)

    # 국내 상장사만 유지(해외종목 제외). code_to_company도 함께 정리.
    kept = {k: v for k, v in companies.items() if is_domestic_company(k, v)}
    kept_keys = set(kept)
    code_to_company = {c: k for c, k in code_to_company.items() if k in kept_keys}
    return kept, code_to_company


def gen_company_nodes(companies: dict) -> list:
    """회사 노드 TTL 라인 생성."""
    lines = []
    for key, info in companies.items():
        uri = company_uri(key)
        parts = [f"{uri} a fin:Company ;"]
        # 표시명
        name_esc = info["name"].replace('"', '\\"')
        parts.append(f'    rdfs:label "{name_esc}"@ko ;')
        # 종목코드들 (속성)
        for code in info["codes"]:
            parts.append(f'    fin:stockCode "{code}" ;')
        # 우선주 등 별칭
        for al in info["aliases"]:
            al_esc = al.replace('"', '\\"')
            parts.append(f'    fin:aliasName "{al_esc}"@ko ;')
        # 마지막 세미콜론을 점으로
        parts[-1] = parts[-1].rstrip(" ;") + " ."
        lines.append("\n".join(parts))
    return lines


def gen_holdings_and_issuance(companies: dict, code_to_company: dict,
                              portfolio_path: str, bond_path: str):
    """
    ETF 보유내역(Holding) + 회사 지름길(holds) + 채권 발행관계(issuedBy) 생성.
    반환: (holding_lines, holds_lines, issuedby_lines, stats)
    """
    holding_lines = []
    holds_pairs = set()   # (etf, company_uri) 중복 제거용 지름길
    stats = {"holding": 0, "skip_no_weight": 0, "skip_no_company": 0}

    # --- ETF 보유내역 ---
    wb = openpyxl.load_workbook(portfolio_path, read_only=True)
    hidx = 0
    for sh in wb.sheetnames:
        ws = wb[sh]
        rows = ws.iter_rows(values_only=True)
        next(rows, None)
        for r in rows:
            if len(r) < 7:
                continue
            etf = str(r[0]).strip() if r[0] is not None else ""
            ticker = str(r[1]).strip() if r[1] is not None else ""
            weight = r[6]
            if not (len(ticker) == 6 and ticker.isdigit()):
                continue
            ckey = code_to_company.get(ticker)
            if not ckey:  # 해외필터로 빠진 종목 등
                stats["skip_no_company"] += 1
                continue
            # 비중 없거나 0이면 Holding 제외(현금성/오류)
            try:
                w = float(weight)
            except (TypeError, ValueError):
                w = 0.0
            if w <= 0:
                stats["skip_no_weight"] += 1
                continue

            hidx += 1
            h_uri = f"ex:Holding_{hidx}"
            etf_uri = f"ex:Product_{safe_local(etf)}"
            c_uri = company_uri(ckey)
            holding_lines.append(
                f"{h_uri} a fin:Holding ;\n"
                f"    fin:ofCompany {c_uri} ;\n"
                f'    fin:weight "{w}"^^xsd:decimal .\n'
                f"{etf_uri} fin:hasHolding {h_uri} ."
            )
            holds_pairs.add((etf_uri, c_uri))
            stats["holding"] += 1
    wb.close()

    # holds 지름길 (비중없이 ETF->회사)
    holds_lines = [f"{e} fin:holds {c} ." for e, c in sorted(holds_pairs)]

    # --- 채권 발행관계 ---
    # 패치된 매칭 로직(PD_PBCM 발행사 + 브랜드 접두 통일)을 그대로 사용해
    # 발행사<->회사 정합을 얻고, 채권코드(PD_NO)별로 issuedBy 엣지를 생성한다.
    from resolve_company import parse_issuer, normalize_for_match, match_bond_issuers

    # 발행사명 -> 회사키 매핑 (브랜드 통일 + PD_PBCM 반영)
    issuer_to_company, _ = match_bond_issuers(bond_path, companies)

    issuedby_lines = []
    wb2 = openpyxl.load_workbook(bond_path, read_only=True)
    ws2 = wb2.active
    rows2 = ws2.iter_rows(values_only=True)
    header = list(next(rows2))
    i_no = header.index("PD_NO")
    i_nm = header.index("PD_NM")
    i_pbcm = header.index("PD_PBCM")
    bond_issued = 0
    for r in rows2:
        bond_no = str(r[i_no]).strip() if r[i_no] is not None else ""
        # 발행사 컬럼 우선, 없으면 상품명 파싱 폴백 (match_bond_issuers와 동일 규칙)
        pbcm = r[i_pbcm] if len(r) > i_pbcm else None
        if pbcm and str(pbcm).strip():
            issuer = str(pbcm).strip()
        else:
            issuer = parse_issuer(r[i_nm] or "")
        ckey = issuer_to_company.get(issuer)
        if ckey and bond_no:
            b_uri = f"ex:Bond_{safe_local(bond_no)}"
            c_uri = company_uri(ckey)
            issuedby_lines.append(f"{b_uri} fin:issuedBy {c_uri} .")
            bond_issued += 1
    wb2.close()
    stats["bond_issued"] = bond_issued

    return holding_lines, holds_lines, issuedby_lines, stats


def main():
    companies, code_to_company = load_inputs()

    header = f"""@prefix fin: <{FIN}> .
@prefix ex:  <{EX}> .
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

#################################################################
#  인스턴스: 회사 노드 + ETF 보유내역(Holding) + 채권 발행관계
#  (스키마는 ontology.ttl 참조)
#################################################################
"""

    company_lines = gen_company_nodes(companies)
    bond_path = find_raw_file("PRBD01N001_국내채권마스터")
    portfolio_path = find_raw_file("etf_portfolio.xlsx")
    holding_lines, holds_lines, issued_lines, stats = gen_holdings_and_issuance(
        companies, code_to_company,
        portfolio_path,
        bond_path,
    )

    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GRAPH_DIR / "instances_company.ttl"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n### 회사 노드 ###\n")
        f.write("\n\n".join(company_lines))
        f.write("\n\n### ETF 보유내역(Holding, 비중 포함) ###\n")
        f.write("\n".join(holding_lines))
        f.write("\n\n### ETF 보유 지름길(holds, 비중없음) ###\n")
        f.write("\n".join(holds_lines))
        f.write("\n\n### 채권 발행관계(issuedBy) ###\n")
        f.write("\n".join(issued_lines))
        f.write("\n")

    print("=== 생성 통계 ===")
    print(f"출력 경로: {out_path}")
    print(f"회사 노드: {len(company_lines)}")
    print(f"Holding 노드: {stats['holding']}")
    print(f"holds 지름길: {len(holds_lines)}")
    print(f"issuedBy 관계: {stats['bond_issued']}")
    print(f"(제외) 비중0/없음: {stats['skip_no_weight']}, 회사없음: {stats['skip_no_company']}")


if __name__ == "__main__":
    main()