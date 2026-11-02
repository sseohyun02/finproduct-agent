"""
정확도 집계 — 손으로 채운 label_sheet.csv 를 읽어 지표를 낸다. (API 호출 0회, 비용 0)

채점 규칙
  verdict 열에 다음 중 하나를 적는다.
    O     정답 — 조건에 맞는 결과가 제대로 나왔다
    X     오답 — 결과가 틀렸거나 엉뚱하다
    부분   일부만 맞음 — 조건 일부 누락, 개수 부족 등
    (빈칸) 미채점 — 집계에서 제외

  qtype 별로 보는 관점
    simple / relation  조건에 맞는 상품이 나왔는가
    compare            비교 대상과 우열 판정이 맞는가
    aggregate          공통 보유 종목과 개수가 맞는가
    gate               차단되어야 할 질문이 차단됐는가 (status=unanswerable 이어야 O)
    no_result          결과 0건 안내가 나왔는가 (status=no_result 이어야 O)
    clarify            되묻기가 나왔는가 (status=clarify 이어야 O)

사용법
  python bench\\score.py --sheet bench\\label_sheet.csv
"""

import argparse
import csv
from collections import defaultdict


# qtype 별로 기대되는 status (자동 검증 가능한 유형)
EXPECTED_STATUS = {
    "gate": "unanswerable",
    "no_result": "no_result",
    "clarify": "clarify",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", required=True)
    args = ap.parse_args()

    with open(args.sheet, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    scored = [r for r in rows if (r.get("verdict") or "").strip()]
    blank = total - len(scored)

    print("=" * 62)
    print("정확도 집계")
    print("=" * 62)
    print(f"  전체 {total}문항 / 채점 {len(scored)}문항 / 미채점 {blank}문항")
    if blank:
        print(f"  [주의] 미채점 {blank}건은 집계에서 제외했습니다.")
        ids = [r["question_id"] for r in rows if not (r.get("verdict") or "").strip()]
        print(f"         {', '.join(ids[:20])}{' ...' if len(ids) > 20 else ''}")

    if not scored:
        print("\n  verdict 열이 전부 비어 있습니다. O / X / 부분 으로 채운 뒤 다시 실행하세요.")
        return

    def norm(v):
        v = (v or "").strip().upper()
        if v in ("O", "0", "OK", "정답"):
            return "O"
        if v in ("X", "오답"):
            return "X"
        if "부분" in v or v == "P":
            return "부분"
        return v

    # ── 전체 ──
    cnt = defaultdict(int)
    for r in scored:
        cnt[norm(r.get("verdict"))] += 1
    n = len(scored)
    strict = cnt["O"] / n * 100                      # 부분점수 없음
    lenient = (cnt["O"] + 0.5 * cnt["부분"]) / n * 100  # 부분 0.5점

    print()
    print("  [전체]")
    print(f"    정답 {cnt['O']}  오답 {cnt['X']}  부분 {cnt['부분']}")
    print(f"    정확도(엄격, 부분=0점) : {strict:.1f}%")
    print(f"    정확도(완화, 부분=0.5) : {lenient:.1f}%")

    # ── 유형별 ──
    print()
    print("  [질문 유형별]")
    print(f"    {'유형':<10} {'n':>3} {'정답':>4} {'오답':>4} {'부분':>4}  {'정확도':>7}")
    print("    " + "-" * 44)
    by_type = defaultdict(list)
    for r in scored:
        by_type[r.get("qtype", "")].append(r)
    for t in sorted(by_type):
        sub = by_type[t]
        c = defaultdict(int)
        for r in sub:
            c[norm(r.get("verdict"))] += 1
        acc = c["O"] / len(sub) * 100
        print(f"    {t:<10} {len(sub):>3} {c['O']:>4} {c['X']:>4} {c['부분']:>4}  {acc:>6.1f}%")

    # ── status 자동 검증 (gate / no_result / clarify) ──
    print()
    print("  [분기 정확도 — status 자동 검증]")
    any_auto = False
    for t, want in EXPECTED_STATUS.items():
        sub = [r for r in rows if r.get("qtype") == t and (r.get("status") or "").strip()]
        if not sub:
            continue
        any_auto = True
        hit = sum(1 for r in sub if (r.get("status") or "").strip() == want)
        print(f"    {t:<10} {hit}/{len(sub)} 이 status={want} ({hit/len(sub)*100:.1f}%)")
        for r in sub:
            got = (r.get("status") or "").strip()
            if got != want:
                print(f"      [불일치] {r['question_id']} status={got or '(없음)'} :: {r['question']}")
    if not any_auto:
        print("    해당 유형 문항이 없습니다.")

    # ── 오차단 검사: 답변해야 할 질문이 차단됐는지 ──
    print()
    print("  [오차단 검사 — 정상 질의가 게이트에 걸렸는가]")
    answerable = [r for r in rows if r.get("qtype") in ("simple", "relation", "compare", "aggregate")]
    wrong = [r for r in answerable if (r.get("status") or "").strip() == "unanswerable"]
    if answerable:
        rate = len(wrong) / len(answerable) * 100
        print(f"    정상 질의 {len(answerable)}건 중 오차단 {len(wrong)}건 ({rate:.1f}%)")
        for r in wrong:
            print(f"      [오차단] {r['question_id']} :: {r['question']}")
    else:
        print("    해당 문항이 없습니다.")

    # ── 오답 목록 ──
    bad = [r for r in scored if norm(r.get("verdict")) in ("X", "부분")]
    if bad:
        print()
        print("  [오답·부분 목록]")
        for r in bad:
            print(f"    {norm(r.get('verdict')):<4} {r['question_id']:>4} {r['question']}")
            if (r.get("note") or "").strip():
                print(f"         이유: {r['note']}")

    print()
    print("=" * 62)
    print(f"  발표용 한 줄: 자체 골든셋 n={n}, 정확도 {strict:.1f}%"
          f"{f' (부분점수 적용 {lenient:.1f}%)' if cnt['부분'] else ''}")
    print("=" * 62)


if __name__ == "__main__":
    main()
