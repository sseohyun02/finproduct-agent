"""
fin-013 규모 지표 집계 스크립트 (프로젝트 루트에서 실행)

  python count_metrics.py

출력
  1) 온톨로지: 클래스 / ObjectProperty / DatatypeProperty 수
  2) 그래프: 클래스별 인스턴스 노드, 관계별 트리플, 합계
  3) 사전: 개념 수, unanswerable 게이트 수, 그룹/별칭 규칙 수
"""

import glob
import json
import os
from collections import Counter

from rdflib import Graph, RDF, RDFS, OWL, URIRef

NS = "http://mirae-agent.example/ontology#"
DATA_NS = "http://mirae-agent.example/data#"


def count_ontology(path="ontology.ttl"):
    g = Graph()
    g.parse(path, format="turtle")
    cls = set(g.subjects(RDF.type, OWL.Class)) | set(g.subjects(RDF.type, RDFS.Class))
    obj = set(g.subjects(RDF.type, OWL.ObjectProperty))
    dat = set(g.subjects(RDF.type, OWL.DatatypeProperty))

    print("=" * 60)
    print("[1] 온톨로지 스키마")
    print("=" * 60)
    print(f"  클래스: {len(cls)}종")
    for c in sorted(str(x).split("#")[-1] for x in cls):
        print(f"    - {c}")
    print(f"  관계(ObjectProperty): {len(obj)}종")
    for p in sorted(str(x).split("#")[-1] for x in obj):
        print(f"    - {p}")
    print(f"  속성(DatatypeProperty): {len(dat)}종")
    for p in sorted(str(x).split("#")[-1] for x in dat):
        print(f"    - {p}")
    return obj


def count_graph(pattern="data/graph/*.ttl", obj_props=None):
    g = Graph()
    files = sorted(glob.glob(pattern))
    for f in files:
        g.parse(f, format="turtle")

    # 인스턴스 노드만 집계(data 네임스페이스). 온톨로지 클래스 URI는 제외
    uris = {str(t) for s, p, o in g for t in (s, o)
            if isinstance(t, URIRef) and str(t).startswith(DATA_NS)}
    by_prefix = Counter(u.split("#")[-1].split("_")[0] for u in uris)

    typed = Counter(str(o).split("#")[-1] for s, p, o in g.triples((None, RDF.type, None)))
    preds = Counter(str(p).split("#")[-1] for s, p, o in g)

    obj_names = {str(x).split("#")[-1] for x in (obj_props or set())}
    rel_total = sum(v for k, v in preds.items() if k in obj_names)

    print()
    print("=" * 60)
    print("[2] 그래프 규모")
    print("=" * 60)
    print(f"  적재 파일: {len(files)}개 -> {[os.path.basename(f) for f in files]}")
    print(f"  전체 트리플: {len(g):,}")
    print()
    print("  인스턴스 노드 (URI 접두별):")
    for k, v in sorted(by_prefix.items(), key=lambda x: -x[1]):
        print(f"    {k:<10} {v:>8,}")
    print(f"    {'합계':<10} {sum(by_prefix.values()):>8,}")
    print()
    print("  rdf:type 이 명시된 노드 (클래스별):")
    for k, v in typed.most_common():
        print(f"    {k:<10} {v:>8,}")
    print(f"    {'합계':<10} {sum(typed.values()):>8,}")
    print()
    print("  관계 트리플 (ObjectProperty 만):")
    for k in sorted(obj_names):
        print(f"    {k:<16} {preds.get(k, 0):>8,}")
    print(f"    {'합계':<16} {rel_total:>8,}")
    print()
    print("  그 외 述語(속성/라벨):")
    for k, v in preds.most_common():
        if k not in obj_names:
            print(f"    {k:<16} {v:>8,}")


def count_dictionary(d="data/dictionary"):
    print()
    print("=" * 60)
    print("[3] 사전·규칙")
    print("=" * 60)
    c = json.load(open(os.path.join(d, "concepts.json"), encoding="utf-8"))
    print(f"  concepts.json 개념: {len(c)}종 -> {list(c.keys())}")

    u = json.load(open(os.path.join(d, "unanswerable.json"), encoding="utf-8"))
    pats = sum(len(gate["패턴"]) for gate in u["게이트"])
    print(f"  unanswerable 게이트: {len(u['게이트'])}종 / 패턴 {pats}개")
    for gate in u["게이트"]:
        print(f"    - {gate['유형']}: 패턴 {len(gate['패턴'])}개")

    gw = json.load(open(os.path.join(d, "group_whitelist.json"), encoding="utf-8"))
    groups = gw.get("그룹", {})
    print(f"  group_whitelist 그룹: {len(groups)}개 / 계열사 {sum(len(v) for v in groups.values())}개")

    ig = json.load(open(os.path.join(d, "issuer_group_rules.json"), encoding="utf-8"))
    fin = ig.get("금융그룹", {})
    ind = ig.get("산업그룹", {})
    print(f"  issuer_group_rules: 금융그룹 {len(fin)}개 / 산업그룹 {len(ind)}개")

    alias = c.get("운용사", {}).get("별칭", {})
    print(f"  운용사 별칭: 기관 {len(alias)}개 / 별칭 {sum(len(v) for v in alias.values())}개")


if __name__ == "__main__":
    obj = count_ontology()
    count_graph(obj_props=obj)
    count_dictionary()
