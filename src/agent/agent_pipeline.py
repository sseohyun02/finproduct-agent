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
from datetime import datetime, timedelta, timezone
from temporal_policy import TemporalPolicy


class FinancialAgent:
    def __init__(self, concepts_path, unanswerable_path, intent_prompt_path,
                 graph=None, duckdb_con=None, clova=None):
        from query_builder import QueryBuilder
        self.qb = QueryBuilder(concepts_path, graph=graph, duckdb_con=duckdb_con)
        self.concepts = json.load(open(concepts_path, encoding="utf-8"))
        self.unanswerable = json.load(open(unanswerable_path, encoding="utf-8"))
        self.clova = clova  # ClovaClient (None이면 의도추출/답변 생략)
        self.temporal_policy = TemporalPolicy(duckdb_con)

    # =========================================================
    # [1] unanswerable 게이트
    # =========================================================
    def check_unanswerable(self, question):
        """답변불가 질문 감지. 걸리면 (True, 안내문구), 아니면 (False, None)."""
        for gate in self.unanswerable["게이트"]:
            for pattern in gate["패턴"]:
                if pattern in question:
                    return True, gate["안내문구"]
        return False, None

    # =========================================================
    # [5] 결과 -> 요약 텍스트 (답변생성 입력용)
    # =========================================================
    def summarize_result(self, result, intent):
        """쿼리 결과를 답변생성용 텍스트로 정리. 값을 명확히 가공해 환각 방지."""
        rtype = result.get("type")
        if rtype == "clarify":
            return self._build_clarify(result.get("concepts", []))
        if rtype == "intersection":
            lines = [f"{name}: {cnt}개 상품이 보유" for name, cnt in result["data"][:10]]
            return "공통 보유 종목:\n" + "\n".join(lines)
        elif rtype == "table":
            df = result["data"]
            if df is None or len(df) == 0:
                return "현재 조건에 일치하는 상품을 찾을 수 없습니다."
            return self._format_table(df, intent["product_scope"])
        elif rtype == "sql_only":
            return f"[DuckDB 미연결] 생성된 SQL: {result['sql']}"
        elif rtype == "codes":
            return f"해당 상품 {len(result['data'])}건 (코드: {result['data'][:10]})"
        return str(result)

    def _build_clarify(self, concepts):
        """상품군 불명확 시: 각 상품군별 간단 개요 + 되묻기.
        환헤지처럼 여러 상품군에 걸친 개념일 때, 어디에 있는지 먼저 알려주고 되묻는다."""
        concept_names = [c.get("concept", "") for c in concepts]

        # 환헤지가 조건에 있으면 상품군별 실태를 안내
        if "환헤지" in concept_names:
            overview = (
                "환헤지(환율 변동 위험 제거) 상품은 상품군에 따라 다릅니다:\n"
                "- 국내ETF: 환헤지 상품 있음 (상품명에 (H) 표기, 약 227개)\n"
                "- 공모펀드: 환헤지 상품 많음 (약 8,475개, 전용 구분 있음)\n"
                "- 해외ETF: 환헤지 상품 없음\n"
                "- 채권: 환헤지 개념 없음"
            )
            ask = "어느 상품군의 환헤지 상품을 찾으시나요? (국내ETF / 공모펀드)"
            return overview + "\n\n" + ask

        # 일반적인 상품군 불명확
        overview = (
            "찾으시는 조건이 여러 상품군에 해당할 수 있습니다. 상품군은 다음과 같습니다:\n"
            "- 국내ETF: 국내 상장 ETF\n"
            "- 해외ETF: 해외 상장 ETF\n"
            "- 공모펀드: 자산운용사 운용 펀드\n"
            "- 채권: 국내 채권"
        )
        ask = "어느 상품군에서 찾으시나요?"
        return overview + "\n\n" + ask

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
    def _build_holdings_response(self, result, intent):
        """구성종목 조회 결과를 가공해 직접 답변한다."""
        status = result.get("status", "error")

        # 상품 미확인·데이터 부족·미지원 등의 안내를 그대로 반환
        if status != "ok":
            return {
                "status": status,
                "answer": result.get(
                    "message",
                    "구성종목 조회 결과를 확인하지 못했습니다.",
                ),
                "intent": intent,
                "result_type": "holdings",
                "retrieved_context": "",
                "think_trace": (
                    "ETF 구성종목 조회를 시도했으나 "
                    f"결과 상태가 '{status}'여서 안내문을 반환함."
                ),
            }

        product_name = result["product_name"]
        product_code = result["product_code"]
        total_count = result["total_count"]
        returned_count = result["returned_count"]
        order = result.get("sort_order")

        order_label = {
            "desc": "비중 높은 순",
            "asc": "비중 낮은 순",
        }.get(order, "회사명 순")

        lines = [
            f"{product_name} ({product_code})",
            (
                f"저장된 회사별 보유내역 {total_count}개 중 "
                f"{returned_count}개를 {order_label}으로 표시합니다."
            ),
            "",
        ]

        for index, row in enumerate(result["data"], start=1):
            lines.append(
                f"{index}. {row['company_name']} "
                f"— 비중 저장값: {row['weight']}"
            )

        lines.extend([
            "",
            "비중 단위가 확인되지 않아 저장된 숫자를 그대로 표시했습니다.",
            result["coverage_note"],
            self.temporal_policy.answer_note(intent),
        ])

        # answer_note의 마지막 문장은 LLM용 지시이므로
        # 사용자에게는 그 앞의 기준일 정보만 표시한다.
        lines[-1] = lines[-1].split(
            ". 조회 결과는 저장된 데이터이며",
            1,
        )[0]

        answer_text = "\n".join(lines)

        return {
            "status": "ok",
            "answer": answer_text,
            "intent": intent,
            "result_type": "holdings",
            "retrieved_context": answer_text,
            "think_trace": (
                "상품 DB에서 ETF를 확인하고, 그래프의 "
                "hasHolding·ofCompany·weight 관계를 조회함. "
                "회사별 비중을 합산한 뒤 정렬과 개수 제한을 적용함."
            ),
        }
        
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
        today = datetime.now(
            timezone(timedelta(hours=9))
        ).date().isoformat()

        intent = self.clova.extract_intent(
            question,
            temporal_context={
                "current_date": today,
                "snapshot_dates": self.temporal_policy.context(),
            },
        )
        if not isinstance(intent, dict) or not intent:
            # 질문에 상품군 키워드가 있으면 "조건 파악했으나 결과 없음"으로 구분,
            # 없으면 일반 가이드. (의도추출 실패와 빈 결과를 다르게 안내)
            detected_scope = None
            for kw, label in [("채권", "채권"), ("ETF", "ETF"), ("etf", "ETF"),
                              ("펀드", "펀드"), ("펀드", "펀드")]:
                if kw in question:
                    detected_scope = label
                    break
            if detected_scope:
                guide = (f"'{detected_scope}' 관련 질문으로 이해했으나, 구체적인 조건을 "
                         f"파악하지 못했습니다. 어떤 기준(수익률·보수·위험등급·규모 등)으로 "
                         f"찾으실지 조금 더 구체적으로 알려주세요.")
                trace = (f"질문에서 상품군({detected_scope})은 감지했으나, "
                         f"구체적 필터/관계 조건을 intent로 변환하지 못함.")
            else:
                guide = ("질문을 더 구체적으로 해주시면 정확히 안내할 수 있습니다. "
                         "예를 들어 어떤 상품군(채권·ETF·펀드)인지, "
                         "어떤 기준(수익률·보수·위험등급·규모 등)으로 찾으실지 알려주세요. "
                         "예: '보수 낮은 미국 ETF', '위험등급 높은 채권', '규모 큰 주식형 펀드'")
                trace = "질문 의도를 특정 조건으로 변환하지 못함(주관적·모호한 표현일 수 있음)."
            return {"status": "error", "answer": guide,
                    "retrieved_context": "", "think_trace": trace}
        
        temporal_result = self.temporal_policy.check(intent)

        if temporal_result is not None:
            status, message = temporal_result

            return {
                "status": status,
                "answer": message,
                "intent": intent,
                "retrieved_context": "",
                "think_trace": "질문의 시간 조건과 데이터 제공 범위를 확인함.",
            }
        
        if verbose:
            print("의도:", json.dumps(intent, ensure_ascii=False))

        # [3][4] 쿼리빌더 + 실행
        try:
            result = self.qb.execute(intent)
        except Exception as e:
            return {"status": "error", "answer": f"쿼리 실행 오류: {e}", "intent": intent,
                    "retrieved_context": "", "think_trace": f"쿼리 실행 중 오류: {e}"}
        
        # 구성종목 결과는 전용 응답으로 처리
        if result.get("type") == "holdings":
            return self._build_holdings_response(result, intent)
        
        # [5] 답변생성
        summary = self.summarize_result(result, intent)

        # clarify(상품군 불명확): 개요+되묻기를 그대로 답으로. CLOVA 거치지 않음(왜곡 방지).
        if result.get("type") == "clarify":
            return {"status": "clarify", "answer": summary,
                    "intent": intent, "result_type": "clarify",
                    "retrieved_context": "",
                    "think_trace": "상품군이 특정되지 않아(scope=all) 상품군별 개요를 안내하고 되물음."}

        # 빈 결과(의도는 파악됐으나 조건에 맞는 데이터 없음):
        # CLOVA를 거치면 엉뚱한 안내(가이드/투자조언)를 생성할 수 있으므로 직접 안내.
        if self._is_empty_result(result):
            trace = self._build_think_trace(intent, result)
            trace += "\n→ 조건에 맞는 상품이 조회되지 않음 (의도는 정상 파악됨)."
            return {"status": "no_result", "answer": summary,
                    "intent": intent, "result_type": result.get("type"),
                    "sql": result.get("sql"),
                    "retrieved_context": self._build_retrieved_context(result, summary),
                    "think_trace": trace}

        notes = [
            self.get_answer_note(intent),
            self.temporal_policy.answer_note(intent),
        ]
        note = " ".join(item for item in notes if item)

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

    def _is_empty_result(self, result):
        """쿼리 결과가 비어있는지 판별. 의도 파악 성공 + 데이터 없음 구분용."""
        rtype = result.get("type")
        if rtype == "table":
            df = result.get("data")
            return df is None or len(df) == 0
        if rtype == "codes":
            return not result.get("data")
        if rtype == "intersection":
            return not result.get("data")
        return False

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