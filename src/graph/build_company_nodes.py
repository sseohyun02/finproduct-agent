"""회사 노드 JSON 생성 (2026 미래에셋 AI Festival)

build_ttl.py의 회사노드 구축 로직(load_inputs)을 재사용해
company_nodes.json / code_to_company.json 을 data/processed/에 저장한다.

build_group.py가 company_nodes.json을 읽으므로(각 회사의 'norm' 등 참조),
그래프 재구축 시 이 스크립트를 먼저 실행해 회사노드를 새 데이터로 갱신한다.

실행: python -m src.graph.build_company_nodes
  (또는 build_ttl.py와 같은 폴더에서 실행)

[2026-08 신규] 기존엔 company_nodes.json이 수동 생성되어 저장 코드가 없었음.
  새 데이터 반영을 위해 저장 스크립트를 명시적으로 추가.
"""
import json
from pathlib import Path
import sys
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import ROOT
PROC_DIR = ROOT / "data" / "processed"


def _jsonable(companies: dict) -> dict:
    """set 필드(codes/aliases/bond_issuer_names)를 list로 변환해 JSON 저장 가능하게."""
    out = {}
    for key, info in companies.items():
        out[key] = {
            "id": info["id"],
            "name": info.get("name", key),
            "norm": info.get("norm", ""),
            "codes": sorted(info.get("codes", set())),
            "aliases": sorted(info.get("aliases", set())),
            "bond_issuer_names": sorted(info.get("bond_issuer_names", set())),
        }
    return out


def main():
    # build_ttl의 회사노드 구축 로직 재사용(동일 결과 보장)
    if __package__:
        from .build_ttl import load_inputs
    else:
        from build_ttl import load_inputs
    companies, code_to_company = load_inputs()

    PROC_DIR.mkdir(parents=True, exist_ok=True)

    # company_nodes.json
    comp_path = PROC_DIR / "company_nodes.json"
    with open(comp_path, "w", encoding="utf-8") as f:
        json.dump(_jsonable(companies), f, ensure_ascii=False, indent=2)

    # code_to_company.json (종목코드 -> 회사키)
    c2c_path = PROC_DIR / "code_to_company.json"
    with open(c2c_path, "w", encoding="utf-8") as f:
        json.dump(code_to_company, f, ensure_ascii=False, indent=2)

    print(f"회사 노드: {len(companies)}개 → {comp_path}")
    print(f"코드 매핑: {len(code_to_company)}개 → {c2c_path}")


if __name__ == "__main__":
    main()