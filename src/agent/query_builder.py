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
import sys
from pathlib import Path

# Docker/README의 src/agent에서 실행하는 진입점도 공통 식별 모듈을 사용한다.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.graph.company_identity import CompanyRegistry
from src.agent.intent_validation import IntentError
from src.agent.query_execution import execute_intent, verified_weight_metadata


class QueryBuilder:
    def __init__(self, concepts_path, graph=None, duckdb_con=None):
        with open(concepts_path, encoding="utf-8") as stream:
            self.concepts = json.load(stream)
        self.graph = graph          # rdflib Graph (SPARQL용)
        self.con = duckdb_con       # DuckDB connection (SQL용)
        self.company_registry = CompanyRegistry()
        self._company_index = None

        # 브랜드 별칭 로드: concepts.json 운용사 별칭 중 영문 브랜드 -> 상품명 LIKE 대상
        # 예: TIGER, KODEX, RISE, ACE, SOL ... (상품명에 항상 포함되므로 LIKE가 정확)
        self.brand_aliases = set()
        for official, alist in self.concepts.get("운용사", {}).get("별칭", {}).items():
            for a in alist:
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9\-]*", a):  # 순수 영문 = 브랜드
                    self.brand_aliases.add(a)

        # 상품군 -> DuckDB 테이블/코드컬럼/코드변환
        self.table_map = {
            "bond": {"table": "bond", "code_col": "pd_no", "code_expr": "pd_no"},
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
            "bond": ["pd_nm", "std_pd_mcls_nm", "srfc_irt", "risk_grade_num",
                     "mat_dt", "bond_credit_grade", "corp_pretax_yield"],
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
    def _resolve_company(self, target):
        """대표명·별칭으로 실제 회사 URI를 찾는다. URI를 입력 이름으로 추측하지 않는다."""
        from rdflib import Namespace
        from rdflib.namespace import RDF, RDFS

        if self.graph is None:
            return None
        if self._company_index is None:
            fin = Namespace("http://mirae-agent.example/ontology#")
            index = {}
            for node in self.graph.subjects(RDF.type, fin.Company):
                names = [*self.graph.objects(node, RDFS.label),
                         *self.graph.objects(node, fin.aliasName)]
                for name in names:
                    key = self.company_registry.match_key(str(name))
                    if key:
                        index.setdefault(key, set()).add(node)
            self._company_index = index
        key = self.company_registry.match_key(target)
        matches = self._company_index.get(key, set())
        if len(matches) > 1:
            raise ValueError(f"회사명 '{target}'에 여러 회사가 매칭됩니다. 회사 대응표를 확인하세요.")
        return next(iter(matches), None)

    def build_graph_query(self, cond, scope=None):
        """
        graph_condition 하나를 SPARQL로 변환해 상품코드 집합 반환.
        cond: {relation, target, direction}
        scope: product_scope. hasTheme이 bond일 때 테마ETF→회사→채권 경로 분기에 사용.
        반환: 상품코드 set (예: {'305080', ...}) 또는 회사코드(교집합용)
        """
        rel = cond["relation"]
        target = cond["target"]
        direction = cond.get("direction", "reverse")

        if rel in {"holds", "issuedBy", "sameGroupAs"}:
            company = self._resolve_company(target)
            if company is None:
                return set()
            company_ref = company.n3()

        if rel == "holds":
            # 대상 회사를 담은 상품 (reverse)
            q = self.PREFIX + f"""SELECT ?p WHERE {{
                ?p fin:holds {company_ref} . }}"""
        elif rel == "hasTheme":
            # 테마는 ETF 상품에 붙음. scope가 bond면 테마ETF→편입회사→발행채권으로 내려감.
            if scope == "bond":
                q = self.PREFIX + f"""SELECT DISTINCT ?p WHERE {{
                    ?etf fin:hasTheme ex:Theme_{self._uri(target)} .
                    ?etf fin:holds ?c .
                    ?p fin:issuedBy ?c . }}"""
            else:
                # etf scope(또는 미지정): 테마가 직접 붙은 ETF
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
                ?p fin:issuedBy {company_ref} . }}"""
        elif rel == "sameGroupAs":
            # 대상과 같은 그룹인 계열사가 발행/보유 (4홉)
            q = self.PREFIX + f"""SELECT ?p WHERE {{
                {company_ref} fin:belongsToGroup ?g .
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
        """지정 테마 ETF 전체에 공통으로 포함된 회사만 반환한다."""
        from rdflib import Namespace
        from rdflib.namespace import RDFS
        fin = Namespace("http://mirae-agent.example/ontology#")
        ex = Namespace("http://mirae-agent.example/data#")
        candidates = set(self.graph.subjects(fin.hasTheme, ex[f"Theme_{self._uri(cond['target'])}"]))
        codes = self.filter_codes_by_scope({str(p).split("Product_", 1)[-1] for p in candidates}, "etf_domestic")
        products = {p for p in candidates if str(p).split("Product_", 1)[-1] in codes}
        if any(not list(self.graph.objects(p, fin.holds)) for p in products):
            raise IntentError("일부 ETF의 구성종목 정보가 없어 전체 공통 종목을 확정할 수 없습니다.", "data_missing")
        if not products:
            return []
        common = set.intersection(*(set(self.graph.objects(p, fin.holds)) for p in products))
        return sorted((str(self.graph.value(c, RDFS.label) or c), len(products)) for c in common)

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
        if self.con is None:
            raise IntentError("상품군 판별에 필요한 DB가 없습니다.", "data_missing")
        info = self.table_map[scope]
        extra = " AND upper(trim(pd_grp_no)) = 'ETF'" if scope == "etf_domestic" else ""
        valid = {str(r[0]) for r in self.con.execute(
            f"SELECT {info['code_expr']} FROM {info['table']} WHERE row_valid = true{extra}"
        ).fetchall()}
        return set(codes) & valid

    # =========================================================
    # 2. SQL 필터/정렬 생성
    # =========================================================
    def resolve_column(self, concept, scope, period=None):
        """개념 + 상품군 -> DuckDB 컬럼명. concepts.json 참조."""
        c = self.concepts.get(concept, {})
        if concept == "수익률":
            return c.get("기간별컬럼", {}).get(period or "1년", {}).get(scope) or (c.get("채권수익률", {}).get("컬럼", {}).get(scope) if period is None else None)
        if concept == "만기일":
            return "mat_dt_date" if scope == "bond" else None

        # 상품명 컬럼 (브랜드 LIKE 검색용)
        if concept == "상품명":
            return {"etf_domestic": "pd_nm", "etf_overseas": "pd_nm",
                    "bond": "pd_nm", "fund": "itm_nm"}.get(scope)

        # 만기: 필터는 잔존일수(remaining_days, 숫자)로 처리(날짜타입 충돌 방지)
        #   "만기 N년/개월 이내" -> remaining_days < 일수. 정렬도 잔존일수 사용.
        if concept == "만기":
            if scope != "bond":
                return None
            return "date_diff('day', coalesce(try_cast(info_base_dt AS DATE), try_strptime(cast(info_base_dt AS VARCHAR), '%Y%m%d')::DATE), mat_dt_date)"

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
        # scope 통칭 정규화: etf -> etf_domestic(국내 기본). all은 execute에서 사전 처리됨.
        if scope == "etf":
            scope = "etf_domestic"
        tinfo = self.table_map[scope]
        table = tinfo["table"]
        code_expr = tinfo["code_expr"]

        # 출력 컬럼: 상품군별 핵심만 (SELECT * 지양)
        cols = self.output_columns.get(scope)
        select_cols = [f"{code_expr} AS product_code", *(cols if cols else ["*"])]
        where = ["row_valid = true"]
        if scope == "etf_domestic":
            where.append("upper(trim(pd_grp_no)) = 'ETF'")  # 불량행 제외

        # SQL 필터
        for f in intent.get("sql_filters", []):
            concept = f["concept"]
            val = f["value"]

            # 브랜드 감지: 운용사/상품명 필터 값이 브랜드(TIGER/KODEX 등)면 상품명 LIKE로
            if concept in {"운용사", "상품명"} and f["operator"] in {"eq", "like"} and isinstance(val, str) and val in self.brand_aliases:
                name_col = self.resolve_column("상품명", scope)
                if name_col:
                    where.append(f"{name_col} LIKE {self._sql_literal('%' + val + '%')}")
                    continue

            # 특수 처리 개념(환헤지·신용등급)은 col 없이도 _filter_clause에서 직접 처리
            if concept in ("환헤지", "신용등급"):
                op = f["operator"]
                clause = self._filter_clause(None, op, val, concept, scope)
                where.append(clause)
                continue

            col = self.resolve_column(concept, scope)
            if not col:
                raise IntentError(f"{scope}에서는 {concept} 조건을 처리할 수 없습니다.", "unsupported")
            op = f["operator"]
            where.append(self._filter_clause(col, op, val, concept, scope))

        # 그래프 결과 결합
        if restrict_codes is not None:
            if not restrict_codes:
                where.append("1=0")  # 그래프 결과 없음 -> 빈 결과
            else:
                codes_str = ",".join(self._sql_literal(c) for c in sorted(restrict_codes))
                where.append(f"{code_expr} IN ({codes_str})")

        sql = f"SELECT DISTINCT {', '.join(select_cols)} FROM {table}"
        if where:
            sql += " WHERE " + " AND ".join(where)

        sort = intent.get("sort")
        if sort:
            sort_col = self.resolve_column(sort["concept"], scope, sort.get("period"))
            if not sort_col or sort.get("order") not in {"asc", "desc"}:
                raise IntentError("지원하지 않는 정렬 조건입니다.", "unsupported")
            if sort.get("period"):
                sort_col = self.concepts.get("수익률", {}).get("기간별컬럼", {}).get(sort["period"], {}).get(scope)
                if sort["concept"] != "수익률" or not sort_col:
                    raise IntentError("지원하지 않는 기간 정렬입니다.", "unsupported")
            if sort_col not in select_cols:
                select_cols.append(sort_col)
                sql = sql.replace("SELECT DISTINCT " + ", ".join(select_cols[:-1]), "SELECT DISTINCT " + ", ".join(select_cols), 1)
            sql += f" ORDER BY {sort_col} {sort['order'].upper()} NULLS LAST, product_code ASC"
        else:
            sql += " ORDER BY product_code ASC"
        limit = intent.get("limit") or 3
        if type(limit) is not int or limit < 1:
            raise IntentError("조회 개수는 양의 정수여야 합니다.")
        sql += f" LIMIT {limit}"
        return sql

    @staticmethod
    def _sql_literal(value):
        from math import isfinite
        if type(value) in (int, float):
            if not isfinite(value):
                raise IntentError("유효하지 않은 숫자 조건입니다.")
            return str(value)
        if not isinstance(value, str):
            raise IntentError("지원하지 않는 비교값입니다.")
        return "'" + value.replace("'", "''") + "'"

    def _filter_clause(self, col, op, val, concept, scope):
        operators = {"gte": ">=", "lte": "<=", "gt": ">", "lt": "<", "eq": "="}
        literal = self._sql_literal
        if concept == "환헤지":
            if op != "eq":
                raise IntentError("환헤지는 eq 조건만 지원합니다.", "unsupported")
            yes = val in ("Y", "있음", "예", "환헤지", "헤지", "H")
            no = val in ("N", "없음", "아니오", "환노출", "UH")
            if not (yes or no):
                raise IntentError("환헤지 여부를 해석할 수 없습니다.")
            if scope == "fund":
                return "hdge_fd_yn = " + literal("Y" if yes else "N")
            if scope == "etf_domestic":
                return "pd_nm " + ("LIKE" if yes else "NOT LIKE") + " '%(H)%'"
            raise IntentError("이 상품군의 환헤지 조건은 지원하지 않습니다.", "unsupported")
        if concept == "신용등급":
            if scope != "bond":
                raise IntentError("채권 외 신용등급 검색은 지원하지 않습니다.", "unsupported")
            if isinstance(val, str) and re.fullmatch(r"[A-D]{1,3}[+-]?", val.strip()):
                grades = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
                          "BB+", "BB", "BB-", "B+", "B", "B-", "CCC", "CC", "C", "D"]
                if val.strip() not in grades or op not in operators:
                    raise IntentError("지원하지 않는 신용등급 비교입니다.", "unsupported")
                index = grades.index(val.strip())
                allowed = {"eq": grades[index:index+1], "gte": grades[:index+1], "gt": grades[:index],
                           "lte": grades[index:], "lt": grades[index+1:]}[op]
                return "bond_credit_grade IN (" + ",".join(literal(x) for x in allowed) + ")" if allowed else "1=0"
            col = "bond_credit_rank"
        if concept == "만기":
            val = [self._maturity_to_days(v) for v in val] if isinstance(val, list) else self._maturity_to_days(val)
            if val is None or (isinstance(val, list) and None in val):
                raise IntentError('만기 기간을 해석할 수 없습니다. 특정 연도·날짜는 concept="만기일", operator="between", value=["YYYY-01-01","YYYY-12-31"]로 지정하고, 잔존기간은 "1년"처럼 단위를 명시하세요.')
        if concept == "만기일":
            from datetime import date
            try:
                for v in (val if isinstance(val, list) else [val]):
                    date.fromisoformat(v)
            except (ValueError, TypeError):
                raise IntentError("만기일은 YYYY-MM-DD 형식으로 지정해야 합니다.")
        if op in operators:
            if isinstance(val, str) and not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", val.strip()):
                mapped = self.map_value(concept, scope, val)
                if op != "eq" and len(mapped) > 1:
                    raise IntentError("여러 범주값의 크기 비교는 지원하지 않습니다.", "unsupported")
                if len(mapped) > 1:
                    return f"{col} IN (" + ",".join(literal(x) for x in mapped) + ")"
                val = mapped[0]
            return f"{col} {operators[op]} {literal(val)}"
        if op == "like":
            if not isinstance(val, str):
                raise IntentError("부분일치 값은 문자열이어야 합니다.")
            return f"{col} LIKE {literal('%' + val + '%')}"
        if op == "in" and isinstance(val, list) and val:
            mapped = []
            for item in val:
                mapped.extend(self.map_value(concept, scope, item))
            return f"{col} IN (" + ",".join(literal(x) for x in mapped) + ")"
        if op == "between" and isinstance(val, list) and len(val) == 2:
            return f"{col} BETWEEN {literal(val[0])} AND {literal(val[1])}"
        raise IntentError("지원하지 않는 필터 연산입니다.", "unsupported")

    def _maturity_to_days(self, val):
        """만기 표현을 잔존일수로 변환. '1년'/1->365, '6개월'->180 등."""
        if isinstance(val, (int, float)):
            # 숫자만 오면 '년'으로 간주(1 -> 365)
            return int(val * 365)
        s = str(val)
        m = re.search(r"(\d+)\s*년", s)
        if m:
            return int(m.group(1)) * 365
        m = re.search(r"(\d+)\s*개?월", s)
        if m:
            return int(m.group(1)) * 30
        m = re.search(r"(\d+)\s*일", s)
        if m:
            return int(m.group(1))
        if s.isdigit():
            return int(s) * 365  # 순수 숫자는 년으로
        return None

    # =========================================================
    # 3. 통합 실행
    # =========================================================
    
    def lookup_holdings(self, intent):
        """국내 ETF 하나의 회사별 보유내역을 조회한다."""
        from decimal import Decimal, InvalidOperation

        from rdflib import Namespace
        from rdflib.namespace import RDFS

        def stop(status, message):
            return {
                "type": "holdings",
                "status": status,
                "message": message,
                "data": [],
            }

        # 1. 지원 범위와 입력 확인
        if self.con is None or self.graph is None:
            return stop(
                "error",
                "구성종목 조회에 필요한 상품 DB 또는 그래프가 연결되지 않았습니다.",
            )

        scope = intent.get("product_scope")

        if scope not in {"etf", "etf_domestic"}:
            return stop(
                "unsupported",
                "현재 구성종목 조회는 국내 ETF를 대상으로 지원합니다.",
            )

        conditions = intent.get("graph_conditions") or []

        if len(conditions) != 1:
            return stop(
                "unsupported",
                "현재는 국내 ETF 하나를 지정한 구성종목 조회를 지원합니다. "
                "추가 관계 조건이 포함된 조회는 아직 지원하지 않습니다.",
            )

        condition = conditions[0]

        if (
            condition.get("relation") != "holds"
            or condition.get("direction") != "forward"
        ):
            return stop(
                "unsupported",
                "ETF에서 구성종목을 조회하는 질문인지 확인해 주세요.",
            )

        # 조건을 처리하지 못할 때 임의로 생략하지 않는다.
        if intent.get("sql_filters") or intent.get("aggregation"):
            return stop(
                "unsupported",
                "구성종목 조회에 추가된 필터 또는 집계 조건은 "
                "아직 지원하지 않습니다.",
            )

        target = condition.get("target")

        if not isinstance(target, str) or not target.strip():
            return stop(
                "clarify",
                "조회할 ETF의 정확한 이름이나 6자리 상품코드를 알려주세요.",
            )

        target = target.strip()

        limit = intent.get("limit") or 3

        if type(limit) is not int or limit < 1:
            return stop(
                "clarify",
                "조회할 구성종목 개수는 1 이상의 정수로 지정해 주세요.",
            )

        sort = intent.get("sort")
        order = None

        if sort is not None:
            if not isinstance(sort, dict):
                return stop("clarify", "정렬 조건을 확인해 주세요.")

            if sort.get("concept") != "보유비중":
                return stop(
                    "unsupported",
                    "구성종목 조회에서는 보유비중 정렬을 지원합니다.",
                )

            order = sort.get("order")

            if order not in {"asc", "desc"}:
                return stop(
                    "clarify",
                    "비중을 높은 순으로 볼지, 낮은 순으로 볼지 알려주세요.",
                )

            if sort.get("period") is not None:
                return stop(
                    "unsupported",
                    "구성종목 비중의 기간별 정렬은 지원하지 않습니다.",
                )

        # 2. 정확한 상품명 또는 코드로 ETF 확인
        # 사용자 입력은 SQL 문자열에 직접 삽입하지 않는다.
        products = self.con.execute(
            """
            SELECT DISTINCT
                substr(pd_itm_no_ma, 2) AS product_code,
                pd_nm AS product_name
            FROM etf_domestic
            WHERE row_valid = true
              AND upper(trim(pd_grp_no)) = 'ETF'
              AND (
                  trim(pd_nm) = ?
                  OR trim(substr(pd_itm_no_ma, 2)) = ?
                  OR trim(pd_itm_no_ma) = ?
              )
            LIMIT 2
            """,
            [target, target, target],
        ).fetchall()

        if not products:
            return stop(
                "product_not_found",
                "입력한 이름이나 코드와 정확히 일치하는 국내 ETF를 "
                "찾지 못했습니다. 정확한 상품명이나 6자리 상품코드를 알려주세요.",
            )

        if len(products) > 1:
            return stop(
                "clarify",
                "일치하는 상품이 여러 개입니다. "
                "6자리 상품코드로 지정해 주세요.",
            )

        product_code, product_name = products[0]
        product_code = str(product_code).strip()

        # 3. ETF의 Holding에서 회사와 비중 조회
        fin = Namespace("http://mirae-agent.example/ontology#")
        ex = Namespace("http://mirae-agent.example/data#")
        product_uri = ex[f"Product_{product_code}"]

        holdings = list(
            self.graph.objects(product_uri, fin.hasHolding)
        )

        if not holdings:
            return stop(
                "data_missing",
                f"{product_name} 상품은 확인했지만, "
                "그래프에 조회 가능한 구성종목·비중 연결이 없습니다.",
            )

        metadata = verified_weight_metadata(self)
        companies = {}

        for holding in holdings:
            company_nodes = list(
                self.graph.objects(holding, fin.ofCompany)
            )
            weight_values = list(
                self.graph.objects(holding, fin.weight)
            )

            # 모호하거나 누락된 행을 제외한 채 정상 결과로 보여주지 않는다.
            if len(company_nodes) != 1 or len(weight_values) != 1:
                return stop(
                    "data_error",
                    "일부 보유내역의 회사 또는 비중 정보가 "
                    "누락되거나 중복되어 결과를 확정할 수 없습니다.",
                )

            company = company_nodes[0]

            try:
                weight = Decimal(str(weight_values[0]))
            except (InvalidOperation, ValueError):
                return stop(
                    "data_error",
                    "숫자로 해석할 수 없는 보유비중이 있습니다.",
                )

            if not weight.is_finite() or weight < 0:
                return stop(
                    "data_error",
                    "보유비중에 유효하지 않은 값이 있습니다.",
                )

            if metadata:
                precision = metadata.get("decimal_places")
                if type(precision) is int and 0 <= precision <= 8:
                    weight = weight.quantize(Decimal(1).scaleb(-precision))
                if metadata["weight_unit"] == "fraction":
                    weight *= 100

            company_id = str(company)
            label = self.graph.value(company, RDFS.label)

            if company_id not in companies:
                companies[company_id] = {
                    "company_id": company_id,
                    "company_name": str(label or company_id),
                    "weight": Decimal("0"),
                    "holding_count": 0,
                }

            # 같은 회사로 연결된 여러 보유내역은 회사 단위로 합산한다.
            companies[company_id]["weight"] += weight
            companies[company_id]["holding_count"] += 1

        # 4. 정렬 후 개수 제한
        rows = list(companies.values())

        if order == "desc":
            rows.sort(
                key=lambda row: (
                    -row["weight"],
                    row["company_name"],
                    row["company_id"],
                )
            )
        elif order == "asc":
            rows.sort(
                key=lambda row: (
                    row["weight"],
                    row["company_name"],
                    row["company_id"],
                )
            )
        else:
            rows.sort(
                key=lambda row: (
                    row["company_name"],
                    row["company_id"],
                )
            )

        total_count = len(rows)
        selected = rows[:limit]

        # JSON으로 전달할 수 있도록 Decimal을 문자열로 변환한다.
        data = [
            {
                "company_id": row["company_id"],
                "company_name": row["company_name"],
                "weight": format(row["weight"], "f"),
                "holding_count": row["holding_count"],
            }
            for row in selected
        ]

        return {
            "type": "holdings",
            "status": "ok",
            "product_code": product_code,
            "product_name": str(product_name),
            "total_count": total_count,
            "returned_count": len(data),
            "sort_order": order,
            "weight_unit": "percent" if metadata else "unverified",
            "data": data,
            "coverage_note": (
                "그래프에 저장된 회사별 보유내역입니다. "
                "수집·필터링 범위에 따라 실제 전체 구성종목과 다를 수 있습니다. "
                "동일 회사에 연결된 보유내역의 비중은 합산했습니다."
            ),
        }
        
    def execute(self, intent, temporal_policy=None):
        return execute_intent(self, intent, temporal_policy)
