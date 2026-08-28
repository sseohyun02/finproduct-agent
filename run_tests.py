# -*- coding: utf-8 -*-
import requests
import json

BASE = "http://localhost:8000/answer"

# 테스트 질문들
questions = [
    "위험등급 5 이상 채권",
    "삼성전자 담은 ETF",
    "삼성전자 담은 ETF 중 보수 낮은 거 5개",
    "신한금융그룹이 발행한 채권",
    "반도체 테마 ETF들이 공통으로 담은 종목",
    "이 ETF 구성종목 비중 알려줘",
    "미국에 투자하는 저렴한 ETF 3개",
    "삼성전자와 같은 그룹 계열사가 발행한 채권",
    "안전한 채권 추천",
    "우량한 회사채",
]

for i, q in enumerate(questions, 1):
    print(f"\n{'='*70}")
    print(f"[{i}] {q}")
    print('='*70)
    try:
        r = requests.get(BASE, params={"q": q}, timeout=60)
        data = r.json()
        print(f"상태: {data['status']}")
        # 의도
        if data.get('intent'):
            intent = data['intent']
            print(f"의도: scope={intent.get('product_scope')}, "
                  f"graph={intent.get('graph_conditions')}, "
                  f"sql={intent.get('sql_filters')}, "
                  f"sort={intent.get('sort')}")
        # SQL
        if data.get('sql'):
            sql = data['sql']
            # IN 절 축약
            if len(sql) > 200:
                import re
                sql = re.sub(r"IN \([^)]{100,}\)", "IN (...축약...)", sql)
            print(f"SQL: {sql}")
        # 답변
        print(f"답변: {data['answer'][:300]}")
    except Exception as e:
        print(f"오류: {e}")