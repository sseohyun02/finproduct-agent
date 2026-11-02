"""
FastAPI 엔드포인트 (2026 미래에셋 AI Festival)

대회 평가 스펙:
  GET /answer?question_id=Q-001&question=질의
  응답(JSON) 5필드: question_id, question, retrieved_context, think_trace, answer

실행:
  uvicorn app:app --host 0.0.0.0 --port 8000

의존:
  findb.duckdb (data/processed/), 그래프 TTL (data/graph/),
  concepts.json·unanswerable.json (data/dictionary/), intent_prompt.md
  .env: CLOVA_API_KEY, CLOVA_MODEL
"""

import os
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# --- 경로 설정 (프로젝트 구조에 맞게 조정) ---
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.config import ROOT, GRAPH_DIR, DICT_DIR, DB_PATH
ONTOLOGY = ROOT / "ontology.ttl"
INTENT_PROMPT = ROOT / "src/agent/prompts/intent_prompt.md"

app = FastAPI(title="금융상품 Agent", version="1.0")

# CORS 허용: 브라우저 기반 챗봇 UI에서 이 API를 호출할 수 있도록.
# 대회 평가/개발 단계이므로 전체 허용(*). 필요시 특정 도메인으로 좁힐 것.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# --- 전역 초기화 (앱 시작 시 1회) ---
agent = None


@app.on_event("startup")
def startup():
    global agent
    from rdflib import Graph
    import duckdb
    from src.agent.clova_client import ClovaClient
    from src.agent.agent_pipeline import FinancialAgent

    # 그래프 로드
    g = Graph()
    g.parse(ONTOLOGY, format="turtle")
    for fn in ["instances_company.ttl", "instances_theme.ttl", "instances_group.ttl"]:
        g.parse(os.path.join(GRAPH_DIR, fn), format="turtle")

    # DuckDB 연결 (읽기전용)
    con = duckdb.connect(str(DB_PATH), read_only=True)

    # CLOVA
    clova = ClovaClient(prompt_path=INTENT_PROMPT)

    agent = FinancialAgent(
        os.path.join(DICT_DIR, "concepts.json"),
        os.path.join(DICT_DIR, "unanswerable.json"),
        INTENT_PROMPT,
        graph=g, duckdb_con=con, clova=clova,
    )
    print("Agent 초기화 완료")


@app.get("/answer")
def answer(
    question: str = Query(None, description="금융상품 관련 질문"),
    question_id: str = Query(None, description="질문 식별자(대회 평가용)"),
    q: str = Query(None, description="질문(간이 파라미터, 호환용)"),
):
    """
    질문을 받아 Agent 답변 반환.
    대회 평가 스펙: GET /answer?question_id=Q-001&question=질의
      응답 5필드: question_id, question, retrieved_context, think_trace, answer
    호환: q 파라미터도 허용(로컬 테스트용).
    """
    # question 우선, 없으면 q(호환)
    query_text = question or q
    if agent is None:
        return JSONResponse({"error": "Agent 초기화 중"}, status_code=503)
    if not query_text:
        return JSONResponse({"error": "question 파라미터가 필요합니다"}, status_code=400)

    result = agent.answer(query_text)

    # 대회 스펙 5필드
    return {
        "question_id": question_id or "",
        "question": query_text,
        "retrieved_context": result.get("retrieved_context", ""),
        "think_trace": result.get("think_trace", ""),
        "answer": result["answer"],
    }


@app.get("/health")
def health():
    return {"status": "ok", "agent_ready": agent is not None}
