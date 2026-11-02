"""검증된 상품군별 계획 실행과 결과 수 제한."""
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
import operator

from rdflib import Namespace
from rdflib.namespace import RDF

if __package__:
    from .intent_validation import IntentError, normalize_intent, plans_for, require
else:
    from intent_validation import IntentError, normalize_intent, plans_for, require

FIN = Namespace("http://mirae-agent.example/ontology#")
EX = Namespace("http://mirae-agent.example/data#")


def problem(error, scope=None, trace=None):
    return {"type": "problem", "status": error.status, "message": str(error),
            "scope": scope, "trace": trace or [], "returned_count": 0, "has_more": False}


def validate_plan(qb, plan):
    scope = plan["product_scope"]
    scope_label = {"bond": "채권", "etf_domestic": "국내 ETF", "etf_overseas": "해외 ETF", "fund": "펀드"}[scope]
    conds = plan["graph_conditions"]
    forward = any(c["direction"] == "forward" for c in conds)
    agg = plan.get("aggregation")
    if forward:
        require(scope == "etf_domestic" and len(conds) == 1 and not plan["computed_filters"]
                and not plan["sql_filters"] and not agg,
                "구성종목 조회는 국내 ETF 하나의 보유내역 조회만 지원합니다.", "unsupported")
    if agg and agg["type"] == "intersection":
        require(len(conds) == 1 and conds[0]["relation"] == "hasTheme" and
                scope == "etf_domestic" and not plan["sql_filters"] and not plan["computed_filters"]
                and not plan.get("sort"),
                "공통 구성종목 집계는 국내 ETF의 단일 테마 조건만 지원합니다.", "unsupported")
    if conds or plan["computed_filters"]:
        require(qb.graph is not None, "관계 데이터가 연결되지 않았습니다.", "data_missing")
        require(scope in {"bond", "etf_domestic"},
                "이 상품군의 회사 보유·발행·그룹 관계 데이터는 제공하지 않습니다.", "unsupported")
    for cond in conds:
        rel = cond["relation"]
        require(rel != "holds" or scope == "etf_domestic", "보유 관계 검색은 국내 ETF에 적용할 수 있습니다.", "unsupported")
        require(rel != "issuedBy" or scope == "bond", "발행사 검색은 채권에 적용할 수 있습니다.", "unsupported")
        if rel in {"issuedBy", "sameGroupAs"} or (rel == "holds" and not forward):
            try:
                node = qb._resolve_company(cond["target"])
            except ValueError as exc:
                raise IntentError(str(exc), "clarify") from exc
            require(node is not None, f"'{cond['target']}' 회사를 저장된 그래프에서 식별할 수 없습니다.", "data_missing")
            if rel == "sameGroupAs":
                require(bool(list(qb.graph.objects(node, FIN.belongsToGroup))),
                        f"'{cond['target']}'의 소속 그룹이 저장된 그래프에 확인되지 않습니다.", "data_missing")
        if rel in {"belongsToGroup", "hasTheme"}:
            kind = "Group" if rel == "belongsToGroup" else "Theme"
            node = EX[f"{kind}_{qb._uri(cond['target'])}"]
            require((node, RDF.type, FIN[kind]) in qb.graph or bool(list(qb.graph.subjects(FIN[rel], node))),
                    f"'{cond['target']}' 그룹·테마를 저장된 그래프에서 찾지 못했습니다.", "data_missing")
    for filt in plan["sql_filters"]:
        concept = filt["concept"]
        require(concept in qb.concepts or concept == "상품명", f"정의되지 않은 검색 조건: {concept}")
        supported = qb.resolve_column(concept, scope) or (concept == "환헤지" and scope in {"fund", "etf_domestic"})
        require(bool(supported), f"{scope_label}에서는 '{concept}' 조건을 처리할 수 없습니다.", "unsupported")
    sort = plan.get("sort")
    if sort:
        supported = (forward and sort["concept"] == "보유비중") or qb.resolve_column(sort["concept"], scope)
        require(bool(supported), f"{scope_label}에서는 '{sort['concept']}' 정렬을 지원하지 않습니다.", "unsupported")
        if sort.get("period"):
            require(sort["concept"] == "수익률" and
                    sort["period"] in qb.concepts["수익률"].get("기간별컬럼", {}) and
                    scope in qb.concepts["수익률"]["기간별컬럼"][sort["period"]],
                    "요청한 기간의 정렬 데이터가 없습니다.", "unsupported")
    if plan["computed_filters"]:
        require(scope == "etf_domestic", "회사별 보유비중 합산은 국내 ETF만 지원합니다.", "unsupported")


def verified_weight_metadata(qb):
    """단위 검증은 그래프 파일 지문과 함께 기록해야 재생성 후 잘못 재사용되지 않는다."""
    metadata = Path(__file__).resolve().parents[2] / "data/graph/holdings_metadata.json"
    if not metadata.exists():
        return None
    record = json.loads(metadata.read_text(encoding="utf-8"))
    unit = record.get("weight_unit")
    if unit not in {"percent", "fraction"} or not record.get("evidence"):
        return None
    # 그래프 자체에 동일 지문을 실어야 임의의 다른 그래프에 단위를 적용하지 않는다.
    stamps = list(qb.graph.objects(EX.HoldingsDataset, FIN.sourceSha256))
    stamp = str(stamps[0]) if len(stamps) == 1 else ""
    return record if stamp and stamp == record.get("source_sha256") else None


def verified_weight_unit(qb):
    record = verified_weight_metadata(qb)
    return record["weight_unit"] if record else None


def weight_filter(qb, calc, candidates, unit, decimal_places=None):
    require(unit in {"percent", "fraction"},
            "보유비중 단위가 검증되지 않아 % 합산 조건을 적용할 수 없습니다. 조건을 생략하지 않고 조회를 중단했습니다.",
            "unsupported")
    nodes = set()
    for name in calc["companies"]:
        try:
            node = qb._resolve_company(name)
        except ValueError as exc:
            raise IntentError(str(exc), "clarify") from exc
        require(node is not None, f"'{name}'의 회사 노드가 없습니다.", "data_missing")
        nodes.add(node)
    # 모든 지정 회사를 보유한 ETF만 후보로 선택한다. 별칭 중복은 회사 URI로 제거.
    products = set.intersection(*(set(qb.graph.subjects(FIN.holds, node)) for node in nodes))
    if candidates is not None:
        products = {p for p in products if str(p).split("Product_", 1)[-1] in candidates}
    totals = {}
    compare = {"eq": operator.eq, "lte": operator.le, "gte": operator.ge,
               "lt": operator.lt, "gt": operator.gt}[calc["operator"]]
    threshold = Decimal(str(calc["value"]))
    for product in products:
        sums = {node: Decimal(0) for node in nodes}
        seen = set()
        for holding in qb.graph.objects(product, FIN.hasHolding):
            companies = list(qb.graph.objects(holding, FIN.ofCompany))
            require(len(companies) == 1, "회사 연결이 모호한 보유내역이 있어 합산할 수 없습니다.", "data_error")
            company = companies[0]
            if company not in nodes:
                continue
            values = list(qb.graph.objects(holding, FIN.weight))
            require(len(values) == 1, "합산 대상 회사의 비중이 누락되거나 중복되어 있습니다.", "data_error")
            try:
                value = Decimal(str(values[0]))
            except InvalidOperation as exc:
                raise IntentError("비중을 숫자로 해석할 수 없습니다.", "data_error") from exc
            require(value.is_finite() and value >= 0, "비중 값이 유효하지 않습니다.", "data_error")
            if decimal_places is not None:
                # pykrx float32 저장 과정의 미세 오차만 원본 표시 정밀도로 복원한다.
                value = value.quantize(Decimal(1).scaleb(-decimal_places))
            sums[company] += value * (100 if unit == "fraction" else 1)
            seen.add(company)
        require(seen == nodes, "보유 관계는 있으나 합산 대상 비중이 누락되었습니다.", "data_missing")
        total = sum(sums.values(), Decimal(0))
        require(total <= 100, "합산 비중이 100%를 초과하여 데이터를 확인해야 합니다.", "data_error")
        if compare(total, threshold):
            totals[str(product).split("Product_", 1)[-1]] = str(total)
    return totals


def execute_plan(qb, plan):
    trace = []
    try:
        validate_plan(qb, plan)
        conds = plan["graph_conditions"]
        if any(c["direction"] == "forward" for c in conds):
            result = qb.lookup_holdings(plan)
            result.update(scope=plan["product_scope"], default_limit=plan["default_limit"], limit=plan["limit"])
            result["has_more"] = result.get("total_count", 0) > result.get("returned_count", 0)
            result["trace"] = ["ETF 확인 → 회사별 저장 비중 합산 → 정렬 → 표시 개수 제한"] if result["status"] == "ok" else []
            return result
        agg = plan.get("aggregation")
        if agg and agg["type"] == "intersection":
            rows = qb.build_intersection(conds[0])
            return {"type": "intersection", "status": "ok", "data": rows[:plan["limit"]],
                    "scope": plan["product_scope"], "returned_count": min(len(rows), plan["limit"]),
                    "has_more": len(rows) > plan["limit"], "default_limit": plan["default_limit"],
                    "trace": ["테마 ETF 전체의 공통 보유 회사 교집합 계산"], "limit": plan["limit"]}
        codes = None
        if conds:
            sets = []
            for cond in conds:
                found = qb.build_graph_query(cond, plan["product_scope"])
                sets.append(found)
                trace.append(f"관계 적용: {cond['relation']}({cond['target']}) → {len(found)}개 상품 코드")
            codes = qb.filter_codes_by_scope(set.intersection(*sets), plan["product_scope"])
        calculated = []
        for calc in plan["computed_filters"]:
            metadata = verified_weight_metadata(qb)
            precision = metadata.get("decimal_places") if metadata else None
            require(precision is None or (type(precision) is int and 0 <= precision <= 8),
                    "비중 정밀도 검증 정보를 확인해야 합니다.", "data_error")
            totals = weight_filter(qb, calc, codes, verified_weight_unit(qb), precision)
            codes = set(totals)
            calculated.append(totals)
            trace.append(f"합산 비중 조건 적용: {', '.join(calc['companies'])} {calc['operator']} {calc['value']}%")
        query_plan = dict(plan, limit=plan["limit"] + 1)
        sql = qb.build_sql(query_plan, codes)
        require(qb.con is not None, "상품 DB가 연결되지 않았습니다.", "data_missing")
        data = qb.con.execute(sql).fetchdf()
        more = len(data) > plan["limit"]
        data = data.head(plan["limit"]).copy()
        for i, totals in enumerate(calculated, 1):
            data[f"합산비중{i}(%)"] = data["product_code"].map(totals)
        for filt in plan["sql_filters"]:
            trace.append(f"SQL 필터 적용: {filt['concept']} {filt['operator']} {filt['value']}")
        sort = plan.get("sort")
        trace.append(f"정렬 적용: {sort['concept']} {sort['order']}" if sort else "상품코드순 정렬 (추천 순위 아님)")
        trace.append(f"전체 조건 적용 후 최대 {plan['limit']}개 표시; 추가 결과 {'있음' if more else '없음'}")
        return {"type": "table", "status": "ok", "scope": plan["product_scope"], "sql": sql,
                "data": data, "returned_count": len(data), "has_more": more, "limit": plan["limit"],
                "default_limit": plan["default_limit"], "trace": trace,
                "coverage_note": "그룹·보유 관계는 저장된 범위만 조회했습니다. 검색되지 않은 상품의 노출이 없다는 뜻은 아닙니다." if conds or calculated else ""}
    except IntentError as exc:
        return problem(exc, plan["product_scope"], trace)


def execute_intent(qb, raw, temporal_policy=None):
    try:
        intent = normalize_intent(raw)
    except IntentError as exc:
        return problem(exc)
    results = []
    for plan in plans_for(intent):
        temporal = temporal_policy.check(plan) if temporal_policy else None
        if temporal:
            results.append(problem(IntentError(temporal[1], temporal[0]), plan["product_scope"]))
        else:
            try:
                results.append(execute_plan(qb, plan))
            except Exception as exc:
                results.append(problem(IntentError(f"조회 중 오류가 발생했습니다: {exc}", "error"), plan["product_scope"]))
        results[-1].setdefault("returned_count", 0)
        results[-1].setdefault("has_more", False)
    if len(results) == 1:
        return results[0]
    if intent["limit_mode"] == "total":
        # 서로 다른 상품군의 숫자를 한 순위로 비교하지 않는다. 요청 순서대로 번갈아 배분.
        counts = [0] * len(results)
        remaining = intent["limit"]
        while remaining:
            progressed = False
            for i, result in enumerate(results):
                if remaining and counts[i] < result["returned_count"]:
                    counts[i] += 1
                    remaining -= 1
                    progressed = True
            if not progressed:
                break
        for result, count in zip(results, counts):
            if result["type"] not in {"table", "holdings", "intersection"} or result.get("status") != "ok":
                continue
            result["has_more"] |= result["returned_count"] > count
            result["data"] = result["data"].head(count) if result["type"] == "table" else result["data"][:count]
            result["returned_count"] = count
            result["total_limit_applied"] = True
            result["trace"].append(f"전체 개수 제한에 따라 이 상품군은 {count}개 배분")
    return {"type": "multi", "results": results, "intent": intent,
            "status": "ok" if all(r.get("status") == "ok" for r in results) else "partial"}
