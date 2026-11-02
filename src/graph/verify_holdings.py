"""현재 원본과 그래프를 전수 대조하고 pykrx 비중 단위를 기록한다.

원본은 수정하지 않는다. 확인 성공 시에만 그래프 원본 지문과 검증 JSON을 기록한다.
"""
import hashlib
import json
import math
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
import sys
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import openpyxl
from rdflib import Graph, Namespace

from .company_identity import CompanyRegistry, company_uri, safe_local

from src.config import ROOT
FIN = Namespace("http://mirae-agent.example/ontology#")
EX = Namespace("http://mirae-agent.example/data#")
EXPECTED_HEADER = ("etf_code", "티커", "구성종목명", "계약수", "금액", "시가총액", "비중")


def verify(root=ROOT):
    source = root / "data/raw/etf_portfolio.xlsx"
    graph_path = root / "data/graph/instances_company.ttl"
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    graph_text = graph_path.read_text(encoding="utf-8")
    graph = Graph().parse(data=graph_text, format="turtle")
    companies = json.loads((root / "data/processed/company_nodes.json").read_text(encoding="utf-8"))
    codes = json.loads((root / "data/processed/code_to_company.json").read_text(encoding="utf-8"))
    registry = CompanyRegistry()
    expected, actual = Counter(), Counter()
    examples, reconciled = [], []
    precision_error = 0.0
    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        sheet_count = len(wb.sheetnames)
        for ws in wb:
            rows = ws.iter_rows(values_only=True)
            if tuple(next(rows)) != EXPECTED_HEADER:
                raise ValueError(f"원본 열 구성이 다릅니다: {ws.title}")
            total = Decimal(0)
            market_rows = []
            for row in rows:
                etf, ticker, name, _, _, market_value, raw = row
                if raw is None:
                    continue
                value = float(raw)
                if not math.isfinite(value):
                    raise ValueError(f"비정상 비중: {ws.title}, {ticker}")
                precision_error = max(precision_error, abs(value - round(value, 2)))
                total += Decimal(str(value))
                if isinstance(market_value, (int, float)) and market_value > 0:
                    market_rows.append((float(market_value), value))
                key = codes.get(str(ticker).strip())
                if key and value > 0:
                    uri = company_uri(key, registry, companies[key])[3:]
                    expected[(f"Product_{safe_local(etf)}", uri, str(Decimal(str(value))))] += 1
                if str(etf) == "069500" and str(ticker) in {"005930", "000660"}:
                    examples.append({"sheet": ws.title, "company": name, "stored_weight": value,
                                     "percentage": round(value, 2)})
            # 시가총액 구성비를 독립 계산하여 100 단위라는 점을 검증한다.
            denominator = sum(amount for amount, _ in market_rows)
            if abs(total - 100) < Decimal("0.5") and denominator > 0 and len(market_rows) >= 5:
                error = max(abs(amount / denominator * 100 - weight) for amount, weight in market_rows)
                if error < 0.02:
                    reconciled.append({"sheet": ws.title, "sum_percent": float(total),
                                       "max_percentage_point_error": error})
    finally:
        wb.close()
    if len(reconciled) < 3:
        raise ValueError("시가총액 비율과 비중의 독립 대조 근거가 부족합니다. 단위를 자동 확정하지 않습니다.")
    if precision_error > 0.00001:
        raise ValueError("원본 비중이 소수 둘째 자리 정밀도와 다릅니다. 별도 검토가 필요합니다.")
    for product, holding in graph.subject_objects(FIN.hasHolding):
        company = list(graph.objects(holding, FIN.ofCompany))
        weights = list(graph.objects(holding, FIN.weight))
        if len(company) != 1 or len(weights) != 1:
            raise ValueError(f"모호한 보유내역: {holding}")
        actual[(str(product).split("#")[-1], str(company[0]).split("#")[-1],
                str(Decimal(str(weights[0]))))] += 1
    if actual != expected:
        raise ValueError(f"원본과 그래프 불일치: 누락 {sum((expected-actual).values())}, 초과 {sum((actual-expected).values())}. 회사 TTL을 재생성하세요.")
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
        raise ValueError("검증 도중 원본이 변경되었습니다.")
    report = {
        "weight_unit": "percent", "decimal_places": 2, "source_sha256": source_hash,
        "evidence": "pykrx COMPST_RTO → 비중 → Excel 일곱 번째 열 → fin:weight 무변환 경로 확인. 원본 시가총액/시트별 시가총액 합계×100과 비중을 독립 대조. 원본과 그래프 보유내역 전수 일치.",
        "sheet_count": sheet_count, "matched_holdings": sum(actual.values()),
        "reconciled_sheet_count": len(reconciled), "reconciled_samples": reconciled[:5],
        "max_two_decimal_storage_error": precision_error, "examples": examples,
    }
    # 전체 검증 성공 후 기존 관계는 보존하고 원본 지문만 갱신한다.
    stamp_pattern = r'ex:HoldingsDataset fin:sourceSha256 "[0-9a-f]+" \.\s*'
    old_stamps = list(graph.objects(EX.HoldingsDataset, FIN.sourceSha256))
    if old_stamps and len(re.findall(stamp_pattern, graph_text)) != len(old_stamps):
        raise ValueError("기존 원본 지문 형식을 확인할 수 없습니다. 회사 TTL을 재생성하세요.")
    graph_text = re.sub(stamp_pattern, "", graph_text).rstrip() + f'\n\nex:HoldingsDataset fin:sourceSha256 "{source_hash}" .\n'
    if graph_path.read_text(encoding="utf-8") != graph_text:
        graph_path.write_text(graph_text, encoding="utf-8")
    (root / "data/graph/holdings_metadata.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    result = verify()
    print(json.dumps({k: result[k] for k in ("weight_unit", "sheet_count", "matched_holdings", "reconciled_sheet_count")}, ensure_ascii=True))
