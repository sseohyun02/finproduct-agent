"""회사 식별 공통 정책. 그룹은 명시적 대응표로만 결정한다."""
import json
import re
from pathlib import Path
import sys
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import DICT_DIR
REGISTRY_PATH = DICT_DIR / "company_registry.json"


def safe_local(value):
    value = re.sub(r"[\s&().,/'\"]+", "_", str(value).strip())
    return re.sub(r"_+", "_", value).strip("_")


def normalize_name(raw):
    if not raw:
        return ""
    value = re.sub(r"\(\s*주\s*\)|주식회사|㈜|\(\s*유\s*\)|유한회사", "", str(raw))
    return re.sub(r"[\s·・,\.\-_/()]+", "", value).lower()


# 기존 발행사 매칭의 브랜드 표기 통일을 보존한다. 그룹 추론에는 사용하지 않는다.
BRAND_PREFIX_MAP = {
    "에스케이씨": "SKC", "에스케이": "SK", "엘지": "LG", "엘엑스": "LX",
    "엘에스": "LS", "케이비": "KB", "케이티앤지": "KT&G", "케이티": "KT",
    "케이지": "KG", "케이씨씨": "KCC", "지에스": "GS", "씨제이": "CJ",
    "디엘": "DL", "디비": "DB", "에이치디현대": "HD현대", "에이치디": "HD",
    "에이치엘": "HL", "에이치엠엠": "HMM", "에이치에스효성": "HS효성",
    "에이케이": "AK", "포스코": "POSCO", "비엔케이": "BNK", "제이비": "JB",
    "에스비에스": "SBS", "에스오일": "S-OIL", "에스엔티": "SNT",
    "엔에이치엔": "NHN", "비지에프": "BGF", "오씨아이": "OCI",
}


def normalize_for_match(raw):
    value = normalize_name(raw)
    for prefix in sorted(BRAND_PREFIX_MAP, key=len, reverse=True):
        if value.startswith(prefix):
            value = normalize_name(BRAND_PREFIX_MAP[prefix]) + value[len(prefix):]
            break
    return value


class CompanyRegistry:
    def __init__(self, path=REGISTRY_PATH):
        with open(path, encoding="utf-8") as stream:
            records = json.load(stream)["companies"]
        self.by_name = {}
        self.by_id = {}
        for record in records:
            company_id = record["id"]
            if not re.fullmatch(r"Company_[\w\-]+", company_id):
                raise ValueError(f"잘못된 회사 ID: {company_id}")
            if company_id in self.by_id:
                raise ValueError(f"중복 회사 ID: {company_id}")
            if not normalize_for_match(record["name"]):
                raise ValueError(f"빈 대표 회사명: {company_id}")
            self.by_id[company_id] = record
            for name in [record["name"], *record.get("aliases", [])]:
                norm = normalize_for_match(name)
                if not norm:
                    raise ValueError(f"빈 별칭: {company_id}")
                previous = self.by_name.get(norm)
                if previous and previous["id"] != company_id:
                    raise ValueError(f"회사 별칭 충돌: {name!r}")
                self.by_name[norm] = record

    def lookup(self, name):
        return self.by_name.get(normalize_for_match(name))

    def match_key(self, name):
        record = self.lookup(name)
        return normalize_for_match(record["name"] if record else name)

    def identity(self, name, *, existing=False):
        record = self.lookup(name)
        if record:
            return record["id"], record["name"]
        # 기존 회사 ID는 보존. 신규 발행사만 정규화 키로 결정한다.
        key = str(name).strip() if existing else normalize_for_match(name)
        if not key:
            raise ValueError("빈 회사명으로 ID를 만들 수 없습니다.")
        company_id = "Company_" + safe_local(key)
        if company_id in self.by_id:
            raise ValueError(f"대응표 ID와 미등록 회사 충돌: {name!r}")
        return company_id, str(name).strip()


def company_uri(key, registry=None, info=None):
    registry = registry or CompanyRegistry()
    company_id = (info or {}).get("id") or registry.identity(key, existing=True)[0]
    return "ex:" + company_id


def build_company_match_index(companies, registry=None):
    registry = registry or CompanyRegistry()
    index = {}
    ids = {}
    for key, info in companies.items():
        uri = company_uri(key, registry, info)
        if uri in ids and ids[uri] != key:
            raise ValueError(f"회사 ID 충돌: {ids[uri]!r}, {key!r}")
        ids[uri] = key
        names = {key, info.get("name", key), *info.get("aliases", []),
                 *info.get("bond_issuer_names", [])}
        for name in names:
            norm = registry.match_key(name)
            if not norm:
                continue
            if norm in index and index[norm] != key:
                raise ValueError(f"회사명 정규화 충돌: {name!r}")
            index[norm] = key
    return index
