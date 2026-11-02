"""LLM 출력을 실행 계약으로 변환한다. 미지원 조건을 누락하지 않는다."""
from copy import deepcopy
from math import isfinite
from datetime import date
import re


class IntentError(ValueError):
    def __init__(self, message, status="invalid_intent"):
        super().__init__(message)
        self.status = status


SCOPES = {"bond", "etf_domestic", "etf_overseas", "fund", "etf"}
RELATIONS = {"holds", "hasTheme", "belongsToGroup", "issuedBy", "sameGroupAs"}
OPERATORS = {"eq", "gte", "lte", "gt", "lt", "between", "like", "in"}


def require(condition, message, status="invalid_intent"):
    if not condition:
        raise IntentError(message, status)


def normalize_intent(raw):
    require(isinstance(raw, dict) and bool(raw), "질문을 검색 조건으로 변환하지 못했습니다.")
    intent = deepcopy(raw)
    allowed = {"product_scope", "product_scopes", "graph_conditions", "sql_filters",
               "computed_filters", "sort", "limit", "limit_mode", "aggregation", "temporal",
               "purpose", "unsupported_conditions", "clarification_reason", "output_fields"}
    require(not (set(raw) - allowed), f"정의되지 않은 의도 필드: {sorted(set(raw) - allowed)}")
    if "temporal" in intent:
        temporal = intent["temporal"]
        require(isinstance(temporal, dict), "temporal은 시점 조건 객체여야 합니다.")
        require(not (set(temporal) - {"kind", "start_date", "end_date"}), "지원하지 않는 시점 필드가 있습니다.")
        require(isinstance(temporal.get("kind"), str) and temporal["kind"] in
                {"snapshot", "scheduled", "observed", "prediction", "realtime", "unclear"}, "시점 조건 종류가 잘못되었습니다.")
    scopes = intent.get("product_scopes")
    if scopes is None:
        scope = intent.get("product_scope")
        scopes = [] if scope in (None, "all") else [scope]
    require(isinstance(scopes, list) and all(isinstance(s, str) and s in SCOPES for s in scopes),
            "product_scopes에는 지원하는 상품군 배열이 필요합니다.")
    if not scopes:
        raise IntentError(intent.get("clarification_reason") or "검색할 상품군을 알려주세요.", "clarify")
    # 국내외 동시 요청은 두 상품군을 지정한다. 이전 etf 값은 국내 기본 계약을 유지한다.
    scopes = list(dict.fromkeys("etf_domestic" if s == "etf" else s for s in scopes))
    intent["product_scopes"] = scopes
    intent["product_scope"] = scopes[0] if len(scopes) == 1 else "all"
    limit = intent.get("limit")
    require(limit is None or (type(limit) is int and limit > 0), "표시 개수는 양의 정수 또는 null이어야 합니다.")
    intent["limit"] = limit
    require(isinstance(intent.get("limit_mode", "per_scope"), str) and
            intent.get("limit_mode", "per_scope") in {"per_scope", "total"}, "개수 제한 범위가 잘못되었습니다.")
    intent["limit_mode"] = intent.get("limit_mode", "per_scope")
    if limit is None:
        intent["limit_mode"] = "per_scope"
    for field in ("graph_conditions", "sql_filters", "computed_filters", "unsupported_conditions"):
        value = intent.get(field, [])
        require(isinstance(value, list), f"{field}은 배열이어야 합니다.")
        intent[field] = value
    if intent["unsupported_conditions"] or intent.get("output_fields"):
        raise IntentError("일부 조건·출력 항목은 현재 지원하지 않습니다: " +
                          str(intent["unsupported_conditions"] or intent["output_fields"]), "unsupported")
    require(isinstance(intent.get("purpose", "search"), str) and
            intent.get("purpose", "search") in {"search", "group_exposure"}, "검색 목적이 잘못되었습니다.")
    for cond in intent["graph_conditions"]:
        require(isinstance(cond, dict), "관계 조건은 객체여야 합니다.")
        require(not (set(cond) - {"relation", "target", "direction"}), "지원하지 않는 관계 조건 필드가 있습니다.")
        require(isinstance(cond.get("relation"), str) and cond["relation"] in RELATIONS, "정의되지 않은 관계입니다.")
        target = cond.get("target")
        require(isinstance(target, str) and bool(target.strip()) and target.strip().lower() not in {"none", "null"},
                f"{cond.get('relation')}의 대상명이 비어 있습니다. 질문에서 대상을 다시 해석해야 합니다.")
        cond["target"] = target.strip()
        require(isinstance(cond.get("direction", "reverse"), str) and
                cond.get("direction", "reverse") in {"forward", "reverse"}, "관계의 방향이 잘못되었습니다.")
        cond.setdefault("direction", "reverse")
        require(cond["direction"] == "reverse" or cond["relation"] == "holds",
                "forward는 ETF 구성종목 holds 조회만 지원합니다.", "unsupported")
    for filt in intent["sql_filters"]:
        require(isinstance(filt, dict), "필터는 객체여야 합니다.")
        require(not (set(filt) - {"concept", "operator", "value"}), "지원하지 않는 필터 필드가 있습니다.")
        require(isinstance(filt.get("concept"), str) and bool(filt["concept"]), "필터의 개념명이 필요합니다.")
        require(isinstance(filt.get("operator"), str) and filt["operator"] in OPERATORS and
                filt.get("value") is not None, "필터 연산자 또는 값이 잘못되었습니다.")
        value, op = filt["value"], filt["operator"]
        # 명시적인 날짜 값은 기간으로 해석하지 않고 만기일 계약으로 통일한다.
        if filt["concept"] == "만기":
            items = value if isinstance(value, list) else [value]
            if all(isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in items):
                filt["concept"] = "만기일"
        if filt["concept"] == "만기일":
            require(op in {"eq", "gte", "lte", "gt", "lt", "between", "in"}, "만기일은 날짜 비교 조건이어야 합니다.")
            try:
                for v in (value if isinstance(value, list) else [value]):
                    date.fromisoformat(v)
            except (ValueError, TypeError):
                raise IntentError('만기일은 YYYY-MM-DD 형식이어야 합니다. 특정 연도 만기는 만기일 between 날짜 범위로 다시 해석하세요.')

        require(not isinstance(value, (dict, bool)), "필터 값의 형식이 잘못되었습니다.")
        if op in {"between", "in"}:
            require(isinstance(value, list) and bool(value) and (op != "between" or len(value) == 2),
                    "between은 값 2개, in은 비어 있지 않은 값 배열이 필요합니다.")
        else:
            require(not isinstance(value, list), "비교값에는 단일 값이 필요합니다.")
        values = value if isinstance(value, list) else [value]
        require(all(isinstance(v, (str, int, float)) and not isinstance(v, bool) and
                    (not isinstance(v, float) or isfinite(v)) for v in values), "필터 값이 잘못되었습니다.")
    for calc in intent["computed_filters"]:
        require(isinstance(calc, dict), "계산 조건은 객체여야 합니다.")
        require(not (set(calc) - {"type", "companies", "operator", "value", "unit"}), "지원하지 않는 계산 조건이 있습니다.")
        require(calc.get("type") == "holding_weight_sum", "지정한 계산은 지원하지 않습니다.", "unsupported")
        names = calc.get("companies")
        require(isinstance(names, list) and bool(names) and all(isinstance(n, str) and n.strip() for n in names),
                "합산 대상 회사명이 필요합니다.")
        require(isinstance(calc.get("operator"), str) and calc["operator"] in {"eq", "gte", "lte", "gt", "lt"},
                "합산 비중 비교 연산자가 잘못되었습니다.")
        value = calc.get("value")
        require(type(value) in (int, float) and isfinite(value) and 0 <= value <= 100 and calc.get("unit") == "percent",
                "합산 비중은 0~100의 숫자와 unit=percent로 지정해야 합니다.")
    if intent.get("purpose") == "group_exposure":
        require(any(c["relation"] in {"sameGroupAs", "belongsToGroup"} for c in intent["graph_conditions"]),
                "그룹 노출 조회에는 기준 회사 또는 그룹 관계가 필요합니다.")
    sort = intent.get("sort")
    if sort is not None:
        require(isinstance(sort, dict), "정렬 조건이 잘못되었습니다.")
        require(not (set(sort) - {"concept", "order", "period"}), "지원하지 않는 정렬 조건이 있습니다.")
        require(isinstance(sort.get("concept"), str) and isinstance(sort.get("order"), str) and
                sort["order"] in {"asc", "desc"}, "정렬 항목 또는 방향이 잘못되었습니다.")
        require(sort.get("period") is None or isinstance(sort["period"], str), "정렬 기간은 문자열이어야 합니다.")
    agg = intent.get("aggregation")
    if agg is not None:
        require(isinstance(agg, dict) and set(agg) == {"type"}, "집계 조건이 잘못되었습니다.")
        require(isinstance(agg.get("type"), str) and agg["type"] in {"list", "intersection"},
                "지정한 집계는 현재 지원하지 않습니다.", "unsupported")
    return intent


def plans_for(intent):
    for scope in intent["product_scopes"]:
        plan = deepcopy(intent)
        plan["product_scope"] = scope
        plan["product_scopes"] = [scope]
        plan["default_limit"] = intent["limit"] is None
        plan["limit"] = intent["limit"] or 3
        yield plan
