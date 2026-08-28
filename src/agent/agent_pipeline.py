"""
금융상품 Agent 파이프라인 (2026 미래에셋 AI Festival)

흐름:
  질문
   -> [1] unanswerable 게이트 (답변불가 사전차단)
   -> [2] 의도추출 (CLOVA -> 의도 JSON)
   -> [3] 쿼리빌더 (의도 -> SQL/SPARQL)
   -> [4] 실행 (DuckDB / rdflib)
   -> [5] 답변생성 (CLOVA -> 자연어)
"""

import json
import re


class FinancialAgent:
    def __init__(self, concepts_path, unanswerable_path, intent_prompt_path,
                 graph=None, duckdb_con=None, clova=None):
        from query_builder import QueryBuilder
        self.qb = QueryBuilder(concepts_path, graph=graph, duckdb_con=duckdb_con)
        self.concepts = json.load(open(concepts_path, encoding="utf-8"))
        self.unanswerable = json.load(open(unanswerable_path, encoding="utf-8"))
        self.clova = clova  # ClovaClient (None이면 의도추출/답변 생략)

    # =========================================================
    # [1] unanswerable 게이트
    # =========================================================
    def check_unanswerable(self, question):
        """답변불가 질문 감지. 걸리면 (True, 안내문구), 아니면 (False, None)."""
        for gate in self.unanswerable["게이트"]:
            for pattern in gate["패턴"]:
                if pattern in question:
                    # 구성종목 게이트는 예외(기초지수) 확인
                    if gate["유형"] == "구성종목" and ("기초지수" in question or "지수" in question):
                        continue
                    return True, gate["안내문구"]
        return False, None

    # =========================================================
    # [5] 결과 -> 요약 텍스트 (답변생성 입력용)
    # =========================================================
    def summarize_result(self, result, intent):
        """쿼리 결과를 답변생성용 텍스트로 정리. 값을 명확히 가공해 환각 방지."""
        rtype = result.get("type")
        if rtype == "intersection":
            lines = [f"{name}: {cnt}개 상품이 보유" for name, cnt in result["data"][:10]]
            return "공통 보유 종목:\n" + "\n".join(lines)
        elif rtype == "table":
            df = result["data"]
            if df is None or len(df) == 0:
                return "조건에 맞는 상품이 없습니다."
            return self._format_table(df, intent["product_scope"])
        elif rtype == "sql_only":
            return f"[DuckDB 미연결] 생성된 SQL: {result['sql']}"
        elif rtype == "codes":
            return f"해당 상품 {len(result['data'])}건 (코드: {result['data'][:10]})"
        return str(result)

    def _format_table(self, df, scope):
        """DataFrame을 사람이 읽기 쉬운 텍스트로. 컬럼 한글화 + 단위변환."""
        # 컬럼 한글 라벨 + 포맷터
        col_labels = {
            "pd_nm": "상품명", "itm_nm": "상품명", "PD_NM": "상품명",
            "cu_fund_mgmt_co": "운용사", "cu_charge_rt": "총보수(%)",
            "pd_risk_nm": "위험등급", "risk_grade_num": "위험등급(숫자)",
            "du_last_aum": "순자산", "fd_nast_suma": "순자산", "ISU_BAL_AMT": "발행잔액",
            "wu_inv_rgn": "투자지역", "fd_ivst_rgn_desc": "투자지역",
            "du_er_1y": "1년수익률(%)", "fd_yr1_ern_r": "1년수익률(%)",
            "STD_PD_MCLS_NM": "채권분류", "SRFC_IRT": "표면금리(%)",
            "MAT_DT": "만기일", "bond_credit_grade": "신용등급",
            "CORP_PRETAX_YIELD": "세전수익률(%)", "asset_type_std": "자산군",
        }

        def fmt_aum(v):
            """원 단위 순자산 -> 억원."""
            try:
                v = float(v)
                if v >= 1e8:
                    return f"{v/1e8:,.0f}억원"
                return f"{v:,.0f}원"
            except (TypeError, ValueError):
                return str(v)

        lines = []
        for idx, row in df.head(10).iterrows():
            parts = []
            for col in df.columns:
                label = col_labels.get(col, col)
                val = row[col]
                if col in ("du_last_aum", "fd_nast_suma", "ISU_BAL_AMT"):
                    val = fmt_aum(val)
                elif col == "MAT_DT":
                    # 20251106.0 -> 2025-11-06
                    try:
                        s = str(int(float(val)))
                        val = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
                    except (TypeError, ValueError):
                        pass
                parts.append(f"{label}={val}")
            lines.append(f"{idx+1}. " + ", ".join(parts))
        return "\n".join(lines)

    def get_answer_note(self, intent):
        """정성표현·투자지역 등 답변에 필요한 안내문구 수집."""
        notes = []
        # 정성표현 답변문구
        for f in intent.get("sql_filters", []):
            c = self.concepts.get(f["concept"], {})
            qual = c.get("정성표현", {})
            for expr, body in qual.items():
                if "답변문구" in body:
                    # 실제 적용된 경우만 (간단화: 값 일치 확인 생략)
                    pass
        # 투자지역 이머징 등
        for f in intent.get("sql_filters", []):
            if f["concept"] == "투자지역":
                grp = self.concepts["투자지역"].get("그룹", {}).get(f.get("value"), {})
                if "답변문구" in grp:
                    notes.append(grp["답변문구"])
        return " ".join(notes) if notes else None

    # =========================================================
    # 전체 실행
    # =========================================================
    def answer(self, question, verbose=False):
        # [1] 게이트
        blocked, msg = self.check_unanswerable(question)
        if blocked:
            return {"status": "unanswerable", "answer": msg,
                    "retrieved_context": "", "think_trace": "질문이 데이터 범위를 벗어나 사전 차단됨(unanswerable 게이트)."}

        # [2] 의도추출
        if not self.clova:
            return {"status": "error", "answer": "CLOVA 미연결 - 의도추출 불가",
                    "retrieved_context": "", "think_trace": ""}
        intent = self.clova.extract_intent(question)
        if not intent:
            return {"status": "error", "answer": "질문 의도를 파악하지 못했습니다.",
                    "retrieved_context": "", "think_trace": "의도추출 실패(JSON 파싱 불가)."}
        if verbose:
            print("의도:", json.dumps(intent, ensure_ascii=False))

        # [3][4] 쿼리빌더 + 실행
        try:
            result = self.qb.execute(intent)
        except Exception as e:
            return {"status": "error", "answer": f"쿼리 실행 오류: {e}", "intent": intent,
                    "retrieved_context": "", "think_trace": f"쿼리 실행 중 오류: {e}"}

        # [5] 답변생성
        summary = self.summarize_result(result, intent)
        note = self.get_answer_note(intent)
        answer_text = self.clova.generate_answer(question, summary, note)

        # retrieved_context: 답변 근거가 된 실제 조회 데이터
        retrieved_context = self._build_retrieved_context(result, summary)
        # think_trace: 의도추출 -> 쿼리 -> 실행 과정(그래프 활용 어필)
        think_trace = self._build_think_trace(intent, result)

        return {"status": "ok", "answer": answer_text,
                "intent": intent, "result_type": result.get("type"),
                "sql": result.get("sql"),
                "retrieved_context": retrieved_context,
                "think_trace": think_trace}

    def _build_retrieved_context(self, result, summary):
        """답변 근거가 된 데이터를 문자열로. 대회 retrieved_context 필드용."""
        parts = []
        if result.get("sql"):
            parts.append(f"[실행 SQL]\n{result['sql']}")
        rtype = result.get("type")
        if rtype in ("table", "intersection", "codes"):
            parts.append(f"[조회 결과]\n{summary}")
        return "\n\n".join(parts) if parts else summary

    def _build_think_trace(self, intent, result):
        """의도추출->쿼리->실행 추론 과정. 그래프+SQL 하이브리드 활용을 드러냄."""
        steps = []
        scope = intent.get("product_scope")
        steps.append(f"1) 질문 의도 분석: 대상 상품군='{scope}'")

        graph_conds = intent.get("graph_conditions", [])
        if graph_conds:
            rel_desc = ", ".join(
                f"{c['relation']}({c['target']})" for c in graph_conds
            )
            steps.append(f"2) 지식그래프 관계 순회 필요: {rel_desc} "
                         f"→ SPARQL로 그래프에서 관련 상품/회사 탐색")
        else:
            steps.append("2) 관계 순회 불필요(속성 필터만) → SQL 직접 조회")

        sql_filters = intent.get("sql_filters", [])
        if sql_filters:
            f_desc = ", ".join(
                f"{f['concept']} {f['operator']} {f['value']}" for f in sql_filters
            )
            steps.append(f"3) 속성 필터: {f_desc} → DuckDB SQL 조건 적용")

        sort = intent.get("sort")
        if sort:
            steps.append(f"4) 정렬: {sort['concept']} {sort['order']}")

        agg = intent.get("aggregation")
        if agg:
            steps.append(f"5) 집계: {agg['type']} (그래프 상 여러 상품의 공통 관계 계산)")

        # 그래프+SQL 결합 여부
        if graph_conds and (sql_filters or sort):
            steps.append("→ 그래프 순회 결과(상품 코드)를 SQL 조건과 결합해 최종 필터링")

        return "\n".join(steps)


# =============================================================
# 오프라인 테스트 (CLOVA 없이 게이트+쿼리빌더만)
# =============================================================
if __name__ == "__main__":
    from rdflib import Graph
    g = Graph()
    for f in ["ontology.ttl", "out/instances_company.ttl",
              "out/instances_theme.ttl", "out/instances_group.ttl"]:
        g.parse(f, format="turtle")

    agent = FinancialAgent(
        "concepts.json", "unanswerable.json", "intent_prompt.md",
        graph=g, duckdb_con=None, clova=None,
    )

    # 게이트 테스트
    print("=== unanswerable 게이트 ===")
    for q in ["삼성전자 ETF 구성종목 비중 알려줘",
              "이 펀드 분배금 얼마야",
              "반도체 ETF 앞으로 오를까",
              "위험등급 5 이상 채권"]:
        blocked, msg = agent.check_unanswerable(q)
        print(f"  [{'차단' if blocked else '통과'}] {q}")
        if blocked:
            print(f"        -> {msg[:40]}...")

    # 쿼리빌더 직접 (CLOVA 없이 의도 수동 주입)
    print("\n=== 쿼리빌더 (의도 수동) ===")
    intent = {"product_scope": "bond", "graph_conditions": [],
              "sql_filters": [{"concept": "위험등급", "operator": "gte", "value": 5}],
              "sort": None, "limit": 5}
    r = agent.qb.execute(intent)
    print("  SQL:", r.get("sql"))
