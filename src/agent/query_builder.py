"""
쿼리빌더 (2026 미래에셋 AI Festival 금융상품 Agent)

역할: HyperCLOVA가 추출한 의도 JSON(intent_schema.json 형식)을
      실제 쿼리(SQL/SPARQL)로 변환하는 결정론적 코드.
      LLM은 '이해'만, 쿼리 생성은 이 코드가 담당(환각 방지).

처리 흐름:
  1) graph_conditions 있으면 -> SPARQL로 상품코드 집합 획득
  2) sql_filters/sort 있으면 -> SQL 생성
  3) 둘 다 있으면 -> 그래프 결과(코드)를 SQL WHERE IN으로 결합

의존:
  concepts.json  : 개념 -> 컬럼/값매핑/정성표현
  DuckDB findb.duckdb : bond/etf_domestic/etf_overseas/fund
  그래프 TTL : rdflib로 로드
"""

import json
import re


class QueryBuilder:
    def __init__(self, concepts_path, graph=None, duckdb_con=None):
        self.concepts = json.load(open(concepts_path, encoding="utf-8"))
        self.graph = graph          # rdflib Graph (SPARQL용)
        self.con = duckdb_con       # DuckDB connection (SQL용)

        # 상품군 -> DuckDB 테이블/코드컬럼/코드변환
        self.table_map = {
            "bond": {"table": "bond", "code_col": "PD_NO", "code_expr": "PD_NO"},
            "etf_domestic": {"table": "etf_domestic", "code_col": "pd_itm_no_ma",
                             "code_expr": "substr(pd_itm_no_ma, 2)"},  # A305080 -> 305080
            "etf_overseas": {"table": "etf_overseas", "code_col": "pd_itm_no",
                             "code_expr": "pd_itm_no"},
            "fund": {"table": "fund", "code_col": "itm_no", "code_expr": "itm_no"},
        }

        # 상품군별 답변용 출력 컬럼 (SELECT * 대신 핵심만 -> 답변 품질/토큰 개선)
        self.output_columns = {
            "etf_domestic": ["pd_nm", "cu_fund_mgmt_co", "cu_charge_rt", "pd_risk_nm",
                             "du_last_aum", "wu_inv_rgn", "du_er_1y"],
            "etf_overseas": ["pd_nm", "cu_fund_mgmt_co", "cu_charge_rt",
                             "du_last_aum", "wu_inv_rgn"],
            "bond": ["PD_NM", "STD_PD_MCLS_NM", "SRFC_IRT", "risk_grade_num",
                     "MAT_DT", "bond_credit_grade", "CORP_PRETAX_YIELD"],
            "fund": ["itm_nm", "asset_type_std", "risk_grade_num",
                     "fd_nast_suma", "fd_yr1_ern_r", "fd_ivst_rgn_desc"],
        }

        # SPARQL prefix
        self.PREFIX = """PREFIX fin:<http://mirae-agent.example/ontology#>
PREFIX ex:<http://mirae-agent.example/data#>
PREFIX rdfs:<http://www.w3.org/2000/01/rdf-schema#>
"""

    # =========================================================
    # 1. 그래프(SPARQL) 조건 -> 상품코드 집합
    # =========================================================
    def build_graph_query(self, cond):
        """
        graph_condition 하나를 SPARQL로 변환해 상품코드 집합 반환.
        cond: {relation, target, direction}
        반환: 상품코드 set (예: {'305080', ...}) 또는 회사코드(교집합용)
        """
        rel = cond["relation"]
        target = cond["target"]
        direction = cond.get("direction", "reverse")

        if rel == "holds":
            # 대상 회사를 담은 상품 (reverse)
            q = self.PREFIX + f"""SELECT ?p WHERE {{
                ?p fin:holds ex:Company_{self._uri(target)} . }}"""
        elif rel == "hasTheme":
            q = self.PREFIX + f"""SELECT ?p WHERE {{
                ?p fin:hasTheme ex:Theme_{self._uri(target)} . }}"""
        elif rel == "belongsToGroup":
            # 그룹 계열사가 발행한 채권 or 담긴 상품
            # 채권: 회사->issuedBy 역방향, ETF: 회사->holds 역방향
            q = self.PREFIX + f"""SELECT ?p WHERE {{
                ?c fin:belongsToGroup ex:Group_{self._uri(target)} .
                {{ ?p fin:issuedBy ?c }} UNION {{ ?p fin:holds ?c }} }}"""
        elif rel == "issuedBy":
            q = self.PREFIX + f"""SELECT ?p WHERE {{
                ?p fin:issuedBy ex:Company_{self._uri(target)} . }}"""
        elif rel == "sameGroupAs":
            # 대상과 같은 그룹인 계열사가 발행/보유 (4홉)
            q = self.PREFIX + f"""SELECT ?p WHERE {{
                ex:Company_{self._uri(target)} fin:belongsToGroup ?g .
                ?c fin:belongsToGroup ?g .
                {{ ?p fin:issuedBy ?c }} UNION {{ ?p fin:holds ?c }} }}"""
        else:
            return set()

        codes = set()
        for row in self.graph.query(q):
            uri = str(row[0])
            code = uri.split("#")[-1]
            # ex:Product_305080 -> 305080, ex:Bond_KRxxx -> KRxxx
            code = re.sub(r"^(Product_|Bond_)", "", code)
            codes.add(code)
        return codes

    def build_intersection(self, cond):
        """반도체 테마 ETF들이 '공통 보유'한 회사 (교집합)."""
        target = cond["target"]
        q = self.PREFIX + f"""SELECT ?cn (COUNT(DISTINCT ?p) as ?n) WHERE {{
            ?p fin:hasTheme ex:Theme_{self._uri(target)} .
            ?p fin:holds ?c . ?c rdfs:label ?cn .
        }} GROUP BY ?cn ORDER BY DESC(?n)"""
        return [(str(r[0]), int(r[1])) for r in self.graph.query(q)]

    def _uri(self, name):
        """회사/테마/그룹명 -> URI 지역명 (한글유지, 특수문자 치환)."""
        s = re.sub(r"[\s&().,/'\"]+", "_", str(name).strip())
        return re.sub(r"_+", "_", s).strip("_")

    def filter_codes_by_scope(self, codes, scope):
        """
        그래프 결과 상품코드를 상품군으로 필터.
        코드 형식으로 판별:
          - 6자리 숫자 = 국내ETF (etf_domestic)
          - KR+숫자(11~12자) 중 KR5=펀드, KR7=국내ETF(pd_itm_no형)
          - KRxxxxxxxxx (채권 PD_NO) = bond
          - 그 외 영문 티커 = 해외ETF
        정밀 구분은 SQL 결합에서 테이블 IN으로 자동 처리되므로,
        여기선 그래프-only 반환 시의 대략 필터.
        """
        def kind(code):
            if re.fullmatch(r"\d{6}", code):
                return "etf_domestic"
            if re.fullmatch(r"KR5\w+", code):
                return "fund"
            if re.fullmatch(r"KR6\w+", code):  # 채권 PD_NO는 KR6 다수
                return "bond"
            if re.fullmatch(r"KR7\w+", code):
                return "etf_domestic"
            if re.fullmatch(r"[A-Z0-9]{2,6}(\.\w)?", code):
                return "etf_overseas"
            return "unknown"

        if scope in ("etf", "all"):
            return codes  # 통칭은 필터 안 함(SQL 결합에서 처리)
        return {c for c in codes if kind(c) == scope}

    # =========================================================
    # 2. SQL 필터/정렬 생성
    # =========================================================
    def resolve_column(self, concept, scope):
        """개념 + 상품군 -> DuckDB 컬럼명. concepts.json 참조."""
        c = self.concepts.get(concept, {})

        # 신용등급: 컬럼이 '종류' 안에 중첩됨 (채권 전용)
        if concept == "신용등급":
            if scope != "bond":
                return None
            # 정성표현('우량한')은 순위컬럼(bond_credit_rank) 사용
            return "bond_credit_rank"

        cols = c.get("컬럼", {})
        # 채권 하위개념(만기/신용등급 등)은 컬럼키가 다를 수 있음
        if scope in cols:
            return cols[scope]
        # bond_만기일 등 접두 키
        for k, v in cols.items():
            if k.startswith(scope):
                return v
        return None

    def map_value(self, concept, scope, value):
        """값매핑 있는 개념(자산군/투자지역 등)은 사용자표현 -> 실제값."""
        c = self.concepts.get(concept, {})
        # 값매핑
        vmap = c.get("값매핑", {})
        if value in vmap and scope in vmap[value]:
            return vmap[value][scope]  # 리스트 반환
        # 투자지역은 '그룹'
        gmap = c.get("그룹", {})
        if value in gmap and scope in gmap[value]:
            return gmap[value][scope]
        return [value]  # 매핑 없으면 원값

    def build_sql(self, intent, restrict_codes=None):
        """
        의도 -> SQL. restrict_codes: 그래프 결과 상품코드(있으면 WHERE IN 결합).
        단일 상품군 기준(여러 상품군이면 호출측에서 UNION).
        """
        scope = intent["product_scope"]
        tinfo = self.table_map[scope]
        table = tinfo["table"]
        code_expr = tinfo["code_expr"]

        # 출력 컬럼: 상품군별 핵심만 (SELECT * 지양)
        cols = self.output_columns.get(scope)
        select_cols = cols if cols else ["*"]
        where = ["row_valid = true"]  # 불량행 제외

        # SQL 필터
        for f in intent.get("sql_filters", []):
            col = self.resolve_column(f["concept"], scope)
            if not col:
                continue  # 이 상품군엔 없는 개념
            op = f["operator"]
            val = f["value"]
            where.append(self._filter_clause(col, op, val, f["concept"], scope))

        # 그래프 결과 결합
        if restrict_codes is not None:
            if not restrict_codes:
                where.append("1=0")  # 그래프 결과 없음 -> 빈 결과
            else:
                codes_str = ",".join(f"'{c}'" for c in restrict_codes)
                where.append(f"{code_expr} IN ({codes_str})")

        sql = f"SELECT {', '.join(select_cols)} FROM {table}"
        if where:
            sql += " WHERE " + " AND ".join(where)

        # 정렬
        sort = intent.get("sort")
        if sort:
            sort_col = self.resolve_column(sort["concept"], scope)
            if sort_col:
                # 수익률은 기간 컬럼
                if sort["concept"] == "수익률" and sort.get("period"):
                    sort_col = self._period_column(sort["period"], scope) or sort_col
                order = sort["order"].upper()
                # 2차 정렬: 동일값 많은 경우 순자산 desc로 안정화
                aum_col = {"etf_domestic": "du_last_aum", "etf_overseas": "du_last_aum",
                           "bond": "ISU_BAL_AMT", "fund": "fd_nast_suma"}.get(scope)
                if aum_col and aum_col != sort_col:
                    sql += f" ORDER BY {sort_col} {order}, {aum_col} DESC"
                else:
                    sql += f" ORDER BY {sort_col} {order}"

        sql += f" LIMIT {intent.get('limit', 10)}"
        return sql

    def _filter_clause(self, col, op, val, concept, scope):
        """단일 필터 -> SQL 조건절."""
        if op in ("gte", "lte", "gt", "lt", "eq"):
            sqlop = {"gte": ">=", "lte": "<=", "gt": ">", "lt": "<", "eq": "="}[op]
            if isinstance(val, str) and not val.isdigit():
                # 값매핑 필요할 수 있음
                mapped = self.map_value(concept, scope, val)
                if len(mapped) == 1:
                    return f"{col} = '{mapped[0]}'"
                else:
                    vals = ",".join(f"'{m}'" for m in mapped)
                    return f"{col} IN ({vals})"
            return f"{col} {sqlop} {val}"
        elif op == "like":
            return f"{col} LIKE '%{val}%'"
        elif op == "in":
            mapped = self.map_value(concept, scope, val) if isinstance(val, str) else val
            vals = ",".join(f"'{m}'" for m in mapped)
            return f"{col} IN ({vals})"
        elif op == "between":
            return f"{col} BETWEEN '{val[0]}' AND '{val[1]}'"
        return "1=1"

    def _period_column(self, period, scope):
        """수익률 기간 -> 컬럼."""
        pc = self.concepts["수익률"].get("기간별컬럼", {})
        return pc.get(period, {}).get(scope)

    # =========================================================
    # 3. 통합 실행
    # =========================================================
    def execute(self, intent):
        """의도 JSON -> 실행 -> 결과."""
        graph_conds = intent.get("graph_conditions", [])
        agg = intent.get("aggregation")

        # 교집합 질의(반도체 테마 공통보유종목)
        if agg and agg.get("type") == "intersection" and graph_conds:
            return {"type": "intersection",
                    "data": self.build_intersection(graph_conds[0])}

        # 그래프 조건 -> 상품코드 집합 (여러 조건이면 교집합)
        restrict_codes = None
        if graph_conds:
            code_sets = [self.build_graph_query(c) for c in graph_conds]
            restrict_codes = set.intersection(*code_sets) if len(code_sets) > 1 else code_sets[0]
            # 상품군 필터 (그래프는 여러 상품군 섞여있음)
            restrict_codes = self.filter_codes_by_scope(restrict_codes, intent["product_scope"])

        # SQL 필요 여부: 필터/정렬이 있거나, 그래프 결과에 상품정보를 붙여야 하면 SQL
        # 그래프-only여도 상품명·속성 표시를 위해 SQL 조회가 기본
        has_graph = restrict_codes is not None
        needs_sql = intent.get("sql_filters") or intent.get("sort") or has_graph or not graph_conds

        # SQL 실행
        sql = self.build_sql(intent, restrict_codes)
        if self.con:
            result = self.con.execute(sql).fetchdf()
            return {"type": "table", "sql": sql, "data": result}
        else:
            return {"type": "sql_only", "sql": sql}  # DuckDB 없으면 SQL만 반환


if __name__ == "__main__":
    # DuckDB 없이 SQL 생성만 테스트
    qb = QueryBuilder("/home/claude/concepts.json")

    tests = [
        {"질문": "위험등급 5 이상 채권",
         "intent": {"product_scope": "bond", "graph_conditions": [],
                    "sql_filters": [{"concept": "위험등급", "operator": "gte", "value": 5}],
                    "sort": None, "limit": 10}},
        {"질문": "미국 투자 보수 낮은 ETF top5",
         "intent": {"product_scope": "etf_domestic", "graph_conditions": [],
                    "sql_filters": [{"concept": "투자지역", "operator": "eq", "value": "미국"}],
                    "sort": {"concept": "총보수", "order": "asc"}, "limit": 5}},
        {"질문": "만기 임박 국공채",
         "intent": {"product_scope": "bond", "graph_conditions": [],
                    "sql_filters": [{"concept": "채권분류", "operator": "eq", "value": "국공채"}],
                    "sort": {"concept": "만기", "order": "asc"}, "limit": 10}},
    ]
    for t in tests:
        print(f"\n[{t['질문']}]")
        r = qb.execute(t["intent"])
        print("  SQL:", r.get("sql"))
