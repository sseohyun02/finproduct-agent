"""
질의 캐시 수집기 — 호출 1회로 '응답시간'과 '채점용 결과'를 동시에 수거한다.

설계 의도
  - CLOVA 호출은 비싸므로 같은 질문을 두 번 부르지 않는다.
    한 번 호출해서 (응답시간 + 답변 + SQL + think_trace)를 전부 캐시에 남기고,
    정확도 채점은 그 캐시로 오프라인에서 한다(추가 비용 0).
  - 결과는 호출 직후 즉시 append + flush 한다. 중단되어도 이전까지는 보존된다.
  - 상한(호출수/토큰)에 걸리면 캐시를 닫고 깨끗하게 멈춘다.
  - 이미 캐시에 있는 (question_id, rep)은 건너뛴다 → 이어받기 가능.

사용법
  # 1차: 30문항 1회 — 채점용 데이터셋 + 응답시간 1샘플
  python bench\\run_cache.py --url http://49.50.134.182:8000 \
      --questions bench\\questions.jsonl --repeat 1

  # 중단됐으면 같은 명령 재실행 → 남은 것만 이어서 호출
  # 응답시간 분포(p50/p95)가 더 필요하면 반복 추가 (비용 발생)
  python bench\\run_cache.py --url http://49.50.134.182:8000 \
      --questions bench\\questions.jsonl --repeat 3

산출물
  bench\\cache.jsonl       질의별 원본 응답 + 응답시간 (이어받기 기준)
  bench\\label_sheet.csv   채점용 시트 (verdict 열을 손으로 채운다)
  bench\\latency.csv       응답시간만 추린 표
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time

import requests


# =============================================================
# 캐시 입출력
# =============================================================
def load_cache(path):
    """기존 캐시를 읽어 (question_id, rep) 키 집합과 전체 레코드를 돌려준다."""
    done, rows = set(), []
    if not os.path.exists(path):
        return done, rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue  # 중단 중 깨진 마지막 줄은 버린다
            done.add((r.get("question_id"), r.get("rep")))
            rows.append(r)
    return done, rows


def append_cache(path, record):
    """레코드 1건을 즉시 기록하고 디스크까지 내린다. 중단 시 손실 방지."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


# =============================================================
# 호출
# =============================================================
def make_session():
    """측정용 세션.
    - trust_env=False : Windows 의 프록시 자동탐지(WPAD)를 끈다.
      이걸 켜두면 localhost 호출에도 1~2초가 얹혀 응답시간이 전부 부풀려진다.
    - Session 재사용   : 커넥션을 유지해 TCP 핸드셰이크 비용을 뺀다.
    """
    s = requests.Session()
    s.trust_env = False
    s.proxies = {}
    return s


def measure_overhead(session, base, n=5):
    """/health 를 반복 호출해 순수 왕복 오버헤드를 잰다. CLOVA 호출 0회, 비용 0.
    여기서 나온 값이 모든 응답시간에 공통으로 섞여 있는 몫이다."""
    vals = []
    for _ in range(n):
        try:
            t0 = time.perf_counter()
            session.get(f"{base}/health", timeout=10)
            vals.append(time.perf_counter() - t0)
        except Exception:
            pass
    if not vals:
        return None
    return round(statistics.median(vals), 4)


def call_once(session, base, q, debug=1, timeout=180):
    """GET /answer 1회. 응답 전체와 응답시간을 함께 담아 돌려준다."""
    params = {"question_id": q["question_id"], "question": q["question"]}
    if debug:
        params["debug"] = 1

    t0 = time.perf_counter()
    resp = session.get(f"{base}/answer", params=params, timeout=timeout)
    elapsed = time.perf_counter() - t0
    resp.raise_for_status()
    body = resp.json()

    m = body.get("metrics") or {}
    return {
        "latency_sec": round(elapsed, 3),
        # 채점에 필요한 것들 — 전부 남긴다
        "answer": body.get("answer", ""),
        "retrieved_context": body.get("retrieved_context", ""),
        "think_trace": body.get("think_trace", ""),
        # 계측 패치가 있으면 채워지고, 없으면 None
        "status": m.get("status"),
        "result_type": m.get("result_type"),
        "sql": m.get("sql"),
        "llm_calls": m.get("llm_calls"),
        "total_tokens": m.get("total_tokens"),
        "clova_sec": m.get("clova_sec"),
        "sparql_sec": m.get("sparql_sec"),
        "sql_sec": m.get("sql_sec"),
    }


# =============================================================
# 산출물 작성
# =============================================================
def infer_status(rec):
    """서버가 metrics.status 를 안 줄 때 think_trace / answer 로 분기를 역산한다.

    agent_pipeline.py 가 분기마다 고유한 think_trace 를 찍기 때문에 가능하다.
    계측 패치를 적용했으면 서버 값이 그대로 쓰이고, 이 함수는 건너뛴다.
    """
    s = (rec.get("status") or "").strip()
    if s and not s.startswith("error:"):
        return s                      # 서버가 준 값 우선
    if s.startswith("error:"):
        return s                      # 호출 자체가 실패한 경우

    tt = rec.get("think_trace") or ""
    ans = rec.get("answer") or ""

    if "unanswerable 게이트" in tt or "사전 차단" in tt:
        return "unanswerable"
    if "상품군이 특정되지 않아" in tt or "되물음" in tt:
        return "clarify"
    if "조회되지 않음" in tt or "찾을 수 없습니다" in ans:
        return "no_result"
    if "변환하지 못함" in tt or "쿼리 실행 중 오류" in tt:
        return "error"
    if tt.strip().startswith("1)"):
        return "ok"
    return "unknown"


def write_label_sheet(path, rows):
    """채점용 시트. verdict / note 열을 손으로 채운다.
    같은 질문이 여러 번 호출됐으면 rep=1 만 채점 대상으로 쓴다."""
    seen, out = set(), []
    for r in sorted(rows, key=lambda x: (x.get("question_id", ""), x.get("rep", 0))):
        qid = r.get("question_id")
        if qid in seen:
            continue
        seen.add(qid)
        out.append(r)

    fields = ["question_id", "qtype", "question", "status", "latency_sec",
              "answer", "sql", "think_trace", "verdict", "note"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in out:
            w.writerow({
                "question_id": r.get("question_id"),
                "qtype": r.get("qtype"),
                "question": r.get("question"),
                "status": infer_status(r),
                "latency_sec": r.get("latency_sec"),
                "answer": (r.get("answer") or "").replace("\n", " / ")[:1000],
                "sql": (r.get("sql") or "")[:500],
                "think_trace": (r.get("think_trace") or "").replace("\n", " / ")[:500],
                "verdict": "",   # O / X / 부분  ← 손으로 채울 칸
                "note": "",      # 틀린 이유      ← 손으로 채울 칸
            })
    return len(out)


def write_latency(path, rows):
    fields = ["question_id", "qtype", "rep", "latency_sec", "status",
              "overhead_sec", "clova_sec", "sparql_sec", "sql_sec"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            row = dict(r)
            row["status"] = infer_status(r)
            w.writerow(row)


def summarize(rows, key):
    vals = sorted(r[key] for r in rows if r.get(key) is not None)
    if not vals:
        return "측정값 없음"
    p95 = vals[max(0, int(len(vals) * 0.95) - 1)]
    return (f"n={len(vals)} 평균={statistics.fmean(vals):.2f} "
            f"p50={statistics.median(vals):.2f} p95={p95:.2f} "
            f"최소={vals[0]:.2f} 최대={vals[-1]:.2f}")


# =============================================================
# 메인
# =============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="서버 주소. 예: http://49.50.134.182:8000")
    ap.add_argument("--questions", required=True)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--outdir", default=None, help="산출물 폴더. 기본은 질문 파일과 같은 폴더")
    ap.add_argument("--sleep", type=float, default=0.5, help="호출 간 대기(초)")
    ap.add_argument("--debug", type=int, default=1)

    # ── 상한: 넘으면 캐시를 닫고 중단 ──
    ap.add_argument("--max-calls", type=int, default=200,
                    help="이번 실행에서 보낼 최대 질의 수. 0이면 무제한")
    ap.add_argument("--max-tokens", type=int, default=500000,
                    help="누적 토큰 상한(계측 패치 있을 때만 동작). 0이면 무제한")
    ap.add_argument("--max-fails", type=int, default=5,
                    help="연속 실패 허용 횟수. 넘으면 중단")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    outdir = args.outdir or os.path.dirname(os.path.abspath(args.questions))
    os.makedirs(outdir, exist_ok=True)
    cache_path = os.path.join(outdir, "cache.jsonl")

    # 질문 로드
    questions = []
    with open(args.questions, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                questions.append(json.loads(line))

    session = make_session()

    # 순수 왕복 오버헤드 측정 (비용 0). 응답시간 해석의 기준선이 된다.
    overhead = measure_overhead(session, base)
    if overhead is not None:
        print(f"왕복 오버헤드(/health 중앙값): {overhead:.4f}초")
        if overhead > 0.3:
            print("  [주의] 오버헤드가 큽니다. 응답시간에 이 값이 공통으로 섞여 있습니다.")
            print("         방화벽·보안프로그램 영향일 수 있으니 결과 해석 시 감안하세요.")
    print()

    done, rows = load_cache(cache_path)
    print(f"질문 {len(questions)}개 × {args.repeat}회 = 총 {len(questions)*args.repeat}건")
    print(f"캐시 기존 {len(rows)}건 → 남은 작업 "
          f"{len(questions)*args.repeat - len(done & {(q['question_id'], r+1) for q in questions for r in range(args.repeat)})}건")
    print(f"상한: 호출 {args.max_calls or '무제한'}건 / 토큰 {args.max_tokens or '무제한'}")
    print("-" * 60)

    calls = 0
    tokens = 0
    fails = 0
    stopped = None

    for rep in range(1, args.repeat + 1):
        for q in questions:
            key = (q["question_id"], rep)
            if key in done:
                continue  # 이미 캐시에 있음 → 건너뜀

            # ── 상한 검사: 호출을 보내기 전에 ──
            if args.max_calls and calls >= args.max_calls:
                stopped = f"호출 상한 도달 ({calls}/{args.max_calls}건)"
                break
            if args.max_tokens and tokens >= args.max_tokens:
                stopped = f"토큰 상한 도달 ({tokens:,}/{args.max_tokens:,})"
                break
            if fails >= args.max_fails:
                stopped = f"연속 실패 {fails}회"
                break

            try:
                res = call_once(session, base, q, debug=args.debug)
                fails = 0
            except Exception as e:
                res = {"latency_sec": None, "answer": "",
                       "status": f"error: {type(e).__name__}: {e}"[:200]}
                fails += 1

            rec = {"question_id": q["question_id"], "rep": rep,
                   "qtype": q.get("qtype", ""), "question": q["question"],
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "overhead_sec": overhead, **res}

            append_cache(cache_path, rec)   # ← 즉시 저장
            rows.append(rec)
            done.add(key)
            calls += 1
            if args.max_tokens and res.get("total_tokens") is None:
                stopped = "응답에 토큰 사용량이 없어 토큰 상한을 검증할 수 없습니다."
                break
            if res.get("total_tokens"):
                tokens += res["total_tokens"]

            print(f"  rep{rep} {q['question_id']:>4} "
                  f"{str(res.get('latency_sec')):>7}s "
                  f"status={str(res.get('status'))[:24]:<24} "
                  f"누적 {calls}건 / {tokens:,}토큰")

            time.sleep(args.sleep)
        if stopped:
            break

    # ── 산출물 ──
    sheet_path = os.path.join(outdir, "label_sheet.csv")
    lat_path = os.path.join(outdir, "latency.csv")
    n_sheet = write_label_sheet(sheet_path, rows)
    write_latency(lat_path, rows)

    print("-" * 60)
    if stopped:
        print(f"[중단] {stopped}")
        print("       캐시는 보존되었습니다. 같은 명령을 다시 실행하면 이어서 호출합니다.")
        print("       상한을 올리려면 --max-calls / --max-tokens 를 조정하세요.")
    else:
        print("[완료] 전체 질의 수집 완료")

    print()
    print(f"이번 실행 호출: {calls}건 / 누적 토큰: {tokens:,}")
    print(f"캐시 전체: {len(rows)}건  →  {cache_path}")
    print(f"채점 시트: {n_sheet}문항  →  {sheet_path}")
    print(f"응답시간:            →  {lat_path}")
    print()
    print("=== 응답시간 ===")
    if overhead is not None:
        print(f" (왕복 오버헤드 {overhead:.4f}초가 아래 값에 공통 포함됨)")
    print(" 전체     :", summarize(rows, "latency_sec"))
    for t in sorted({r.get("qtype", "") for r in rows}):
        sub = [r for r in rows if r.get("qtype") == t]
        print(f" {t:<9}:", summarize(sub, "latency_sec"))

    # gate 경로는 LLM 호출이 0회이므로, 그 값이 곧 'LLM 제외 처리시간'의 상한이다.
    gate = [r for r in rows if r.get("qtype") == "gate" and r.get("latency_sec") is not None]
    ok_rows = [r for r in rows if r.get("qtype") in ("simple", "relation", "compare", "aggregate")
               and r.get("latency_sec") is not None]
    if gate and ok_rows:
        g = statistics.median(r["latency_sec"] for r in gate)
        o = statistics.median(r["latency_sec"] for r in ok_rows)
        print()
        print("=== 분기 효과 (LLM 호출 유무 비교) ===")
        print(f"  게이트 차단 (CLOVA 0회) : p50 {g:.2f}초")
        print(f"  정상 답변  (의도 분석) : p50 {o:.2f}초")
        print(f"  차이                    : {o - g:.2f}초  (LLM 왕복이 차지하는 몫)")

    st = {}
    for r in rows:
        k = infer_status(r)
        st[k] = st.get(k, 0) + 1
    print()
    print("=== status 분포 ===")
    for k, v in sorted(st.items(), key=lambda x: -x[1]):
        print(f"  {k:<30} {v}건")

    print()
    print("다음 단계: label_sheet.csv 의 verdict 열을 O / X / 부분 으로 채운 뒤")
    print("           python bench\\score.py --sheet bench\\label_sheet.csv")

    if stopped:
        sys.exit(2)


if __name__ == "__main__":
    main()