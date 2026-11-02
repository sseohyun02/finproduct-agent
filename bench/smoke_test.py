"""
스모크 테스트 — 측정 전에 '돌아가는지'만 확인한다. (CLOVA 호출 2~4회)

확인 항목
  1. 서버가 살아있는지 (/health)
  2. CLOVA 키가 먹는지 (답변이 나오는지)
  3. 한글 질문이 깨지지 않는지
  4. 게이트 경로가 CLOVA 없이 차단되는지
  5. 응답에 metrics 필드가 오는지 (계측 패치 적용 여부)

사용법
  python bench\\smoke_test.py --url http://49.50.134.182:8000
  python bench\\smoke_test.py --url http://localhost:8000
"""

import argparse
import json
import time

import requests


def pretty(obj, limit=600):
    s = json.dumps(obj, ensure_ascii=False, indent=2)
    return s if len(s) <= limit else s[:limit] + "\n  ...(생략)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="서버 주소. 예: http://49.50.134.182:8000")
    ap.add_argument("--debug", type=int, default=1, help="1이면 metrics 요청")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    ok = True

    # ── 1. health ────────────────────────────────────────────
    print("=" * 60)
    print("[1] /health 확인")
    print("=" * 60)
    try:
        r = requests.get(f"{base}/health", timeout=10)
        print(f"  HTTP {r.status_code} / {r.text}")
        if r.status_code != 200 or not r.json().get("agent_ready"):
            print("  [실패] agent_ready 가 true 가 아닙니다. 서버 로그를 확인하세요.")
            print("         (DuckDB·TTL 로드 실패이거나 아직 초기화 중)")
            ok = False
    except Exception as e:
        print(f"  [실패] 서버에 닿지 않습니다: {type(e).__name__}: {e}")
        print("         - 로컬이면 uvicorn 이 떠 있는지")
        print("         - NCP면 systemctl status fin-agent / ACG 포트 8000 개방 확인")
        return

    # ── 2~5. 질의 테스트 ─────────────────────────────────────
    cases = [
        ("정상 질의 (의도 분석)", "S01", "위험등급 5 이상 채권"),
        ("예측 요청 안내", "G01", "반도체 ETF 앞으로 오를까"),
    ]

    for label, qid, question in cases:
        print()
        print("=" * 60)
        print(f"[{label}] {question}")
        print("=" * 60)
        params = {"question_id": qid, "question": question}
        if args.debug:
            params["debug"] = 1
        try:
            t0 = time.perf_counter()
            r = requests.get(f"{base}/answer", params=params, timeout=180)
            elapsed = time.perf_counter() - t0
            print(f"  HTTP {r.status_code} / 응답시간 {elapsed:.2f}초")
            r.raise_for_status()
            body = r.json()
        except Exception as e:
            print(f"  [실패] {type(e).__name__}: {e}")
            ok = False
            continue

        # 필수 5필드
        need = ["question_id", "question", "retrieved_context", "think_trace", "answer"]
        missing = [k for k in need if k not in body]
        if missing:
            print(f"  [실패] 응답에 빠진 필드: {missing}")
            ok = False
        else:
            print("  필수 5필드: 정상")

        # 한글 확인
        if body.get("question") != question:
            print(f"  [주의] 질문이 변형됨: {body.get('question')!r}")
            print("         한글 인코딩 문제일 수 있습니다.")

        ans = (body.get("answer") or "").strip()
        print(f"  답변({len(ans)}자): {ans[:300]}")
        if not ans:
            print("  [실패] 답변이 비어 있습니다. CLOVA 키·모델명을 확인하세요.")
            ok = False
        elif "오류" in ans or "미연결" in ans:
            print("  [주의] 답변에 오류 문구가 있습니다. 서버 로그를 확인하세요.")

        tt = (body.get("think_trace") or "").strip()
        print(f"  think_trace: {tt[:200]}")

        # metrics 확인
        m = body.get("metrics")
        if m:
            print(f"  metrics: {pretty(m, 400)}")
            if m.get("total_tokens") in (None, 0):
                print("  [주의] total_tokens 가 비어 있습니다.")
                print("         CLOVA 응답에 usage 가 안 오는 경우이며, 토큰은 추정치로만 가능합니다.")
        else:
            print("  metrics: 미제공 — 호출 횟수와 토큰 사용량을 확정할 수 없습니다.")
            print("           토큰 사용량은 이 응답으로 측정하지 않습니다.")
            print("           ")

    print()
    print("=" * 60)
    print("결과:", "통과 — 측정 진행 가능" if ok else "실패 항목 있음 — 위 로그 확인")
    print("=" * 60)


if __name__ == "__main__":
    main()
