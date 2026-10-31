"""명시적 회사 대응표로 그룹을 연결하고, 그룹 미확인 발행사도 보존한다."""
import json
from pathlib import Path
from rdflib import Literal

if __package__:
    from .company_identity import (
        CompanyRegistry, build_company_match_index, company_uri, safe_local,
    )
    from .resolve_company import iter_bond_issuers
    from .build_ttl import find_raw_file
else:
    from company_identity import (
        CompanyRegistry, build_company_match_index, company_uri, safe_local,
    )
    from resolve_company import iter_bond_issuers
    from build_ttl import find_raw_file

FIN = "http://mirae-agent.example/ontology#"
EX = "http://mirae-agent.example/data#"
ROOT = Path(__file__).resolve().parents[2]
GRAPH_DIR = ROOT / "data/graph"
PROC_DIR = ROOT / "data/processed"


def group_uri(name):
    return f"ex:Group_{safe_local(name)}"


def build_group_lines(companies, bond_rows, registry=None):
    """입력 행을 읽어 TTL과 미등록 발행사 검토 목록을 만든다. 파일 쓰기는 main만 수행."""
    registry = registry or CompanyRegistry()
    index = build_company_match_index(companies, registry)
    groups = set()
    belongs = set()
    issued = set()
    standalone = {}
    review = {}
    id_keys = {company_uri(k, registry, v): registry.match_key(k)
               for k, v in companies.items()}

    def add_group(uri, record):
        group = record.get("group") if record else None
        if group:
            groups.add(group)
            belongs.add(f"{uri} fin:belongsToGroup {group_uri(group)} .")

    for key, info in companies.items():
        add_group(company_uri(key, registry, info), registry.lookup(info["name"]))

    for bond_no, issuer in bond_rows:
        norm = registry.match_key(issuer)
        if not norm or not bond_no:
            continue
        record = registry.lookup(issuer)
        company_key = index.get(norm)
        if company_key is not None:
            if not record or not record.get("group"):
                company_id = company_uri(company_key, registry, companies[company_key])[3:]
                item = review.setdefault(company_id, {
                    "id": company_id, "names": set(), "bond_codes": set(),
                    "reason": "unregistered" if not record else "group_unknown",
                })
                item["names"].add(issuer)
                item["bond_codes"].add(bond_no)
            # 회사 TTL이 같은 정규화 기준으로 이 발행 관계를 생성한다.
            continue

        company_id, name = registry.identity(issuer)
        uri = "ex:" + company_id
        if uri in id_keys and id_keys[uri] != norm:
            raise ValueError(f"발행사 ID 충돌: {issuer!r}, {uri}")
        id_keys[uri] = norm
        entry = standalone.setdefault(uri, {"names": set(), "record": record})
        entry["names"].add(name)
        entry["names"].add(issuer)
        add_group(uri, record)
        # 그룹 판정과 독립적으로 발행사 및 발행 관계를 보존한다.
        issued.add(f"ex:Bond_{safe_local(bond_no)} fin:issuedBy {uri} .")
        if not record or not record.get("group"):
            item = review.setdefault(company_id, {
                "id": company_id, "names": set(), "bond_codes": set(),
                "reason": "unregistered" if not record else "group_unknown",
            })
            item["names"].add(issuer)
            item["bond_codes"].add(bond_no)

    nodes = []
    for uri, entry in sorted(standalone.items()):
        record = entry["record"]
        names = entry["names"]
        label = record["name"] if record else min(names, key=lambda x: (len(x), x))
        aliases = names | set(record.get("aliases", []) if record else [])
        nodes.extend([
            f"{uri} a fin:Company .",
            f"{uri} rdfs:label {Literal(label, lang='ko').n3()} .",
        ])
        for alias in sorted(aliases - {label}):
            nodes.append(f"{uri} fin:aliasName {Literal(alias, lang='ko').n3()} .")
        # ETF 회사 목록에 없다는 이유로 isListed=false를 추정하지 않는다.

    header = (f"@prefix fin: <{FIN}> .\n@prefix ex: <{EX}> .\n"
              "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n")
    group_nodes = [f"{group_uri(g)} a fin:Group ; rdfs:label {Literal(g, lang='ko').n3()} ."
                   for g in sorted(groups)]
    ttl = header + "\n".join(group_nodes + nodes + sorted(belongs) + sorted(issued)) + "\n"
    review_rows = [{**item, "names": sorted(item["names"]),
                    "bond_codes": sorted(item["bond_codes"])}
                   for _, item in sorted(review.items())]
    return ttl, review_rows, {"groups": len(groups), "issuers": len(standalone),
                              "belongsToGroup": len(belongs), "issuedBy": len(issued)}


def main():
    with open(PROC_DIR / "company_nodes.json", encoding="utf-8") as stream:
        companies = json.load(stream)
    registry = CompanyRegistry()
    # 이전 중간 JSON이 섞이지 않도록 재생성을 안내하고 중단한다.
    for key, info in companies.items():
        if (info.get("id") != registry.identity(key, existing=True)[0]
                or info.get("norm") != registry.match_key(key)):
            raise ValueError("company_nodes.json이 이전 형식입니다. 먼저 build_company_nodes를 다시 실행하세요.")
    ttl, review, stats = build_group_lines(
        companies, iter_bond_issuers(find_raw_file("PRBD01N001")), registry,
    )
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    (GRAPH_DIR / "instances_group.ttl").write_text(ttl, encoding="utf-8")
    (PROC_DIR / "unmapped_issuers.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"출력: {GRAPH_DIR / 'instances_group.ttl'}")
    print(stats)
    print(f"그룹 미확인 발행사: {len(review)}개 (data/processed/unmapped_issuers.json)")


if __name__ == "__main__":
    main()
