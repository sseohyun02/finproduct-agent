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
if __package__:
    from .temporal_policy import TemporalPolicy
    from .query_builder import QueryBuilder
    from .intent_validation import normalize_intent, IntentError
else:
    from temporal_policy import TemporalPolicy
    from query_builder import QueryBuilder
    from intent_validation import normalize_intent, IntentError


class FinancialAgent:
    def __init__(self, concepts_path, unanswerable_path, intent_prompt_path,
                 graph=None, duckdb_con=None, clova=None):
        self.qb = QueryBuilder(concepts_path, graph=graph, duckdb_con=duckdb_con)
        with open(concepts_path, encoding="utf-8") as stream:
            self.concepts = json.load(stream)
        with open(unanswerable_path, encoding="utf-8") as stream:
            self.unanswerable = json.load(stream)
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
            lines = [f"{name}: {cnt}개 상품이 보유" for name, cnt in result["data"]]
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
            "product_code": "상품코드", "srfc_irt": "표면금리(%)", "pd_nm": "상품명", "itm_nm": "상품명", "PD_NM": "상품명",
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
        for idx, row in df.iterrows():
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
            weight_text = (f"비중: {row['weight']}%" if result.get("weight_unit") == "percent"
                           else f"비중 저장값: {row['weight']}")
            lines.append(
                f"{index}. {row['company_name']} "
                f"— {weight_text}"
            )

        lines.extend([
            "",
            ("원본에서 확인한 퍼센트 단위로 표시했습니다." if result.get("weight_unit") == "percent"
             else "비중 단위가 확인되지 않아 저장된 숫자를 그대로 표시했습니다."),
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

        context = {"current_date": today, "snapshot_dates": self.temporal_policy.context()}
        feedback = None
        intent = None
        result = None
        for attempt in range(2):
            kwargs = {"temporal_context": context}
            if feedback:
                kwargs["validation_feedback"] = feedback
            try:
                raw = self.clova.extract_intent(question, **kwargs)
                if isinstance(raw, dict) and "temporal" not in raw:
                    raise IntentError("필수 시점 조건 temporal이 누락되었습니다. 원문에서 시점을 다시 해석하세요.")
                intent = normalize_intent(raw)
                # 시점 검사는 전체 요청이 아니라 각 상품군의 실제 조회 계획에 적용한다.
                result = self.qb.execute(intent, temporal_policy=self.temporal_policy)
                parts = result.get("results", [result])
                failures = [r["message"] for r in parts if r.get("status") == "invalid_intent"]
                if failures and attempt == 0:
                    feedback = {"previous_intent": raw, "errors": failures}
                    continue
                break
            except IntentError as exc:
                if exc.status == "invalid_intent" and attempt == 0:
                    feedback = {"previous_intent": raw, "errors": [str(exc)]}
                    continue
                result = {"type": "problem", "status": exc.status, "message": str(exc), "trace": []}
                break
            except Exception as exc:
                result = {"type": "problem", "status": "error", "message": f"의도 분석 또는 조회 중 오류가 발생했습니다: {exc}", "trace": []}
                break
        if verbose:
            print("의도:", json.dumps(intent, ensure_ascii=False))
        return self._render_result(result, intent)

    @staticmethod
    def _count_notice(result):
        count = result.get("returned_count", 0)
        if not count:
            if result.get("has_more"):
                return "전체 표시 개수 제한에 따라 이 상품군의 결과는 표시하지 않았습니다. 조회 가능한 결과는 있습니다."
            return "저장된 데이터에서 조건에 일치하는 상품이 조회되지 않았습니다."
        if not result.get("default_limit", False):
            if count < result.get("limit", count) and not result.get("has_more") and not result.get("total_limit_applied"):
                return f"요청하신 {result['limit']}개보다 적은 {count}개가 조건에 맞아, 조회된 상품을 모두 보여드립니다."
            return ""
        if result.get("has_more"):
            return f"조건에 맞는 상품이 여러 개 있어 우선 {count}개를 보여드립니다."
        return f"조건에 맞는 결과 {count}개를 찾았습니다."

    def _render_part(self, result, intent):
        if result["type"] == "problem":
            if result["status"] == "invalid_intent":
                return "질문의 검색 조건을 일관되게 해석하지 못해 조회를 중단했습니다. 조건 일부를 생략한 검색 결과는 제공하지 않았습니다."
            return result["message"]
        if result["type"] == "holdings":
            response = self._build_holdings_response(result, intent)
            if result.get("status") != "ok":
                return response["answer"]
            return "\n".join(text for text in [self._count_notice(result), response["answer"]] if text)
        summary = self.summarize_result(result, intent)
        note = self.temporal_policy.answer_note(intent).split(". 조회 결과는 저장된 데이터이며", 1)[0]
        parts = [self._count_notice(result)]
        if result.get("returned_count"):
            parts.append(summary)
        if result.get("default_limit") and result.get("returned_count") and result["type"] == "table":
            sort = intent.get("sort")
            if sort:
                direction = "낮은" if sort["order"] == "asc" else "높은"
                parts[0] += f" {sort['concept']} 기준으로 {direction} 순서부터 보여드립니다."
            else:
                parts[0] += " 보기 편하도록 상품코드순으로 정리했습니다."
        if result.get("coverage_note"):
            parts.append(result["coverage_note"])
        parts.append(note)
        return "\n\n".join(part for part in parts if part)

    def _render_result(self, result, intent):
        intent = intent or {}
        labels = {"bond": "채권", "etf_domestic": "국내 ETF", "etf_overseas": "해외 ETF", "fund": "펀드"}
        results = result.get("results", [result])
        answers, contexts, traces = [], [], []
        if len(results) > 1:
            if intent.get("limit_mode") == "total":
                answers.append(f"요청하신 전체 {intent['limit']}개 한도에 맞춰 상품군별 결과를 번갈아 골라 보여드립니다.")
            elif intent.get("limit") is None:
                answers.append("상품군별로 최대 3개씩 보여드립니다.")
        if intent.get("purpose") == "group_exposure":
            answers.append("같은 그룹 회사가 발행한 채권과 해당 회사들을 구성종목으로 담은 ETF를 찾았습니다. 추가로 매수하면 같은 그룹에 투자하는 금액이 늘어날 수 있는 상품들입니다.")
        for part in results:
            scope = part.get("scope") or intent.get("product_scope")
            plan = dict(intent, product_scope=scope, product_scopes=[scope])
            text = self._render_part(part, plan)
            title = labels.get(scope, "조회")
            answers.append((title + "\n" if len(results) > 1 else "") + text)
            contexts.append(title + "\n" + self._build_retrieved_context(part, text))
            traces.append(title + "\n" + self._build_think_trace(plan, part))
        statuses = [r.get("status", "ok") for r in results]
        if len(results) == 1:
            status = statuses[0]
            if status == "ok" and not result.get("returned_count", 0):
                status = "no_result"
        else:
            status = "ok" if all(s == "ok" for s in statuses) else "partial"
            if len(set(statuses)) == 1:
                status = statuses[0]
            if all(s == "ok" for s in statuses) and not any(r.get("returned_count") for r in results):
                status = "no_result"
        return {"status": status, "answer": "\n\n".join(answers), "intent": intent,
                "result_type": result["type"], "retrieved_context": "\n\n".join(contexts),
                "think_trace": "\n\n".join(traces), "sql": result.get("sql")}

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
        steps = list(result.get("trace", []))
        if result.get("status", "ok") != "ok":
            steps.append(f"중단 상태: {result.get('status')}; {result.get('message', '')}")
        elif not result.get("returned_count") and not result.get("has_more"):
            steps.append("실행된 조건의 조회 결과가 0건입니다. 원본 전체에 해당 상품이 없다는 뜻은 아닙니다.")
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
