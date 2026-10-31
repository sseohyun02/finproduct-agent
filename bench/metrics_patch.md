# 계측 패치 가이드 (fin-013)

목표: `GET /answer` 응답에 `metrics` 필드를 덧붙여 CLOVA 호출 횟수·토큰·구간별 소요시간을 수거한다.
제출 스펙 5필드(`question_id`, `question`, `retrieved_context`, `think_trace`, `answer`)는 그대로 유지하고,
`metrics`는 **옵션 필드로 추가**한다.

---

## 1. `clova_client.py` — 호출 횟수·토큰 누적

```python
class ClovaClient:
    def __init__(self, prompt_path=None):
        # ... 기존 코드 ...
        self.reset_metrics()

    def reset_metrics(self):
        """질의 1건 시작 시 호출. 누적값 초기화."""
        self.llm_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.clova_sec = 0.0

    def _chat(self, system, user, temperature=0.1, max_tokens=1024):
        """CLOVA chat completions 호출 + 사용량 누적."""
        import time
        t0 = time.perf_counter()
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.clova_sec += time.perf_counter() - t0
        self.llm_calls += 1

        # OpenAI 호환 엔드포인트이므로 usage 가 내려온다.
        # 단, CLOVA 쪽에서 비어 있을 수 있으므로 반드시 방어적으로 읽는다.
        u = getattr(resp, "usage", None)
        if u is not None:
            self.prompt_tokens += getattr(u, "prompt_tokens", 0) or 0
            self.completion_tokens += getattr(u, "completion_tokens", 0) or 0
        else:
            # usage 미제공 시: 입력 문자 수를 기록해 '추정치'로 쓴다 (보고 시 추정 명시)
            self.prompt_tokens += len(system) + len(user)

        return resp.choices[0].message.content

    def dump_metrics(self):
        return {
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "clova_sec": round(self.clova_sec, 3),
        }
```

> 먼저 `print(resp.usage)` 로 **실제 응답에 usage가 오는지 한 번 확인**할 것.
> 비어 있으면 위 fallback(문자 수)이 쓰이므로, 발표 시 "토큰 추정치(문자 수 기준)"로 표기해야 한다.

---

## 2. `query_builder.py` — SPARQL/SQL 구간 계측

```python
class QueryBuilder:
    def __init__(self, concepts_path, graph=None, duckdb_con=None):
        # ... 기존 코드 ...
        self.reset_metrics()

    def reset_metrics(self):
        self.sparql_count = 0
        self.sparql_sec = 0.0
        self.sql_sec = 0.0

    def build_graph_query(self, cond, scope=None):
        # ... 기존 쿼리 문자열 생성 ...
        import time
        t0 = time.perf_counter()
        codes = set()
        for row in self.graph.query(q):
            uri = str(row[0])
            code = re.sub(r"^(Product_|Bond_)", "", uri.split("#")[-1])
            codes.add(code)
        self.sparql_sec += time.perf_counter() - t0
        self.sparql_count += 1
        return codes

    def execute(self, intent):
        # ... 기존 코드 ...
        sql = self.build_sql(intent, restrict_codes)
        if self.con:
            import time
            t0 = time.perf_counter()
            result = self.con.execute(sql).fetchdf()
            self.sql_sec += time.perf_counter() - t0
            return {"type": "table", "sql": sql, "data": result}
        return {"type": "sql_only", "sql": sql}

    def dump_metrics(self):
        return {
            "sparql_count": self.sparql_count,
            "sparql_sec": round(self.sparql_sec, 3),
            "sql_sec": round(self.sql_sec, 3),
        }
```

---

## 3. `agent_pipeline.py` — 질의 시작 시 초기화, 반환에 metrics 합산

`answer()` 맨 앞에 초기화를 넣고, **모든 return 경로**에 metrics를 붙인다.

```python
def answer(self, question, verbose=False):
    import time
    t0 = time.perf_counter()
    if self.clova:
        self.clova.reset_metrics()
    self.qb.reset_metrics()

    def _m(status, result_type=None):
        """모든 반환 경로에서 공통으로 붙일 계측 묶음."""
        m = {"status": status, "result_type": result_type,
             "latency_sec": round(time.perf_counter() - t0, 3)}
        m.update(self.qb.dump_metrics())
        if self.clova:
            m.update(self.clova.dump_metrics())
        else:
            m.update({"llm_calls": 0, "prompt_tokens": 0,
                      "completion_tokens": 0, "total_tokens": 0})
        return m

    # [1] 게이트
    blocked, msg = self.check_unanswerable(question)
    if blocked:
        return {"status": "unanswerable", "answer": msg,
                "retrieved_context": "",
                "think_trace": "질문이 데이터 범위를 벗어나 사전 차단됨(unanswerable 게이트).",
                "metrics": _m("unanswerable")}

    # ... 이하 각 return 에도 "metrics": _m("error") / _m("clarify") /
    #     _m("no_result", result_type=result.get("type")) /
    #     _m("ok", result_type=result.get("type")) 를 추가
```

---

## 4. `app.py` — metrics 를 응답에 노출

```python
@app.get("/answer")
def answer(question: str = Query(None), question_id: str = Query(None),
           q: str = Query(None), debug: int = Query(0)):
    query_text = question or q
    if agent is None:
        return JSONResponse({"error": "Agent 초기화 중"}, status_code=503)
    if not query_text:
        return JSONResponse({"error": "question 파라미터가 필요합니다"}, status_code=400)

    result = agent.answer(query_text)

    payload = {
        "question_id": question_id or "",
        "question": query_text,
        "retrieved_context": result.get("retrieved_context", ""),
        "think_trace": result.get("think_trace", ""),
        "answer": result["answer"],
    }
    # 측정용: debug=1 일 때만 metrics 노출 (제출 스펙 응답은 그대로 유지)
    if debug and result.get("metrics"):
        payload["metrics"] = result["metrics"]
    return payload
```

측정 시에는 하네스의 URL에 `?debug=1` 을 붙이거나, `call_once`의 params에 `"debug": 1` 을 추가한다.

---

## 5. 비교군(LLM이 쿼리를 직접 생성하는 경로) 구성

현재 코드에는 비교군이 없다. "코드 생성 vs LLM 생성" 비교를 하려면 **비교군을 새로 만들어야** 한다.

```python
# clova_client.py 에 추가 — 비교군 전용. 평소 경로에는 쓰지 않는다.
def generate_sql_directly(self, question, schema_hint):
    """비교군: CLOVA가 SQL을 직접 생성. 검증 실패 시 1회 재시도."""
    system = ("너는 DuckDB SQL 생성기다. 아래 스키마만 사용해 SELECT 문 하나만 출력해라.\n"
              + schema_hint)
    sql = self._chat(system, question, temperature=0.1)
    return sql
```

```python
# 비교 실행 스크립트 (측정 전용, 서버에 붙이지 않아도 됨)
import os
MODE = os.getenv("FIN013_QUERY_MODE", "codegen")  # codegen | llmgen

if MODE == "llmgen":
    sql = clova.generate_sql_directly(question, schema_hint)
    try:
        df = con.execute(sql).fetchdf()      # 문법 오류 시 재시도 -> 호출 2회
    except Exception:
        sql = clova.generate_sql_directly(question + "\n(직전 SQL은 문법 오류)", schema_hint)
        df = con.execute(sql).fetchdf()
else:
    sql = qb.build_sql(intent, restrict_codes)   # 현재 방식
    df = con.execute(sql).fetchdf()
```

측정 절차

```bash
# 현재 방식
FIN013_QUERY_MODE=codegen systemctl restart fin-agent
python measure_bench.py --url "http://49.50.134.182:8000/answer?debug=1" \
  --questions questions.jsonl --repeat 3 --label codegen --out bench_codegen.csv

# 비교군
FIN013_QUERY_MODE=llmgen systemctl restart fin-agent
python measure_bench.py --url "http://49.50.134.182:8000/answer?debug=1" \
  --questions questions.jsonl --repeat 3 --label llmgen --out bench_llmgen.csv
```

같은 질문 세트·같은 반복 횟수·같은 시간대에 돌려야 한다. CLOVA API 응답 지연이 시간대에 따라 변한다.
