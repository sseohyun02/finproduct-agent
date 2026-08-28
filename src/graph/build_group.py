import json, re
from urllib.parse import quote
import openpyxl
FIN="http://mirae-agent.example/ontology#"; EX="http://mirae-agent.example/data#"
def safe_local(s):
    s=str(s).strip()
    s=re.sub(r"[\s&().,/'\"]+","_",s)
    return re.sub(r"_+","_",s).strip("_")
def company_uri(k): return f"ex:Company_{safe_local(k)}"
def group_uri(k): return f"ex:Group_{safe_local(k)}"
def issuer_uri(k): return f"ex:Issuer_{safe_local(k)}"

def make_judge(rules):
    fin_groups=rules["금융그룹"]; ind_groups=rules["산업그룹"]
    fin_kw=rules["금융업종키워드"]; ind_kw=fin_kw+rules["제조업종키워드추가"]
    exclude_exact=set(rules["명시적배제"]); exclude_kw=rules["배제키워드"]
    def judge(issuer):
        if issuer in exclude_exact: return None
        if any(kw in issuer for kw in exclude_kw): return None
        for group,prefixes in fin_groups.items():
            for p in prefixes:
                if issuer.startswith(p) and any(kw in issuer for kw in fin_kw): return group
        for group,prefixes in ind_groups.items():
            for p in prefixes:
                if issuer.startswith(p) and any(kw in issuer for kw in ind_kw): return group
        return None
    return judge

companies=json.load(open("/home/claude/out/company_nodes.json",encoding="utf-8"))
wl=json.load(open("/home/claude/group_whitelist.json",encoding="utf-8"))["그룹"]
rules=json.load(open("/home/claude/issuer_group_rules.json",encoding="utf-8"))
judge=make_judge(rules)
company_keys=set(companies.keys())

# 주식회사 -> 그룹
company_to_group={}
for group,members in wl.items():
    for m in members:
        if m in company_keys: company_to_group[m]=group

# 발행사 -> 그룹
from resolve_company import parse_issuer, normalize_name
BOND="PRBD01N001_국내채권마스터_20260711_datarows.xlsx"
wb=openpyxl.load_workbook(BOND,read_only=True); ws=wb.active
rows=ws.iter_rows(values_only=True); header=list(next(rows))
i_no=header.index("PD_NO"); i_nm=header.index("PD_NM")
norm_to_company={info["norm"]:k for k,info in companies.items()}
issuer_to_group={}; issuer_bonds={}
for r in rows:
    bond_no=str(r[i_no]).strip() if r[i_no] else ""
    issuer=parse_issuer(r[i_nm] or "")
    if not issuer or not bond_no: continue
    if normalize_name(issuer) in norm_to_company: continue
    g=judge(issuer)
    if g:
        issuer_to_group[issuer]=g
        issuer_bonds.setdefault(issuer,[]).append(bond_no)
wb.close()

used_groups=set(company_to_group.values())|set(issuer_to_group.values())
group_nodes=[f'{group_uri(g)} a fin:Group ;\n    rdfs:label "{g}"@ko .' for g in sorted(used_groups)]
belongs_company=[f"{company_uri(k)} fin:belongsToGroup {group_uri(g)} ." for k,g in sorted(company_to_group.items())]
issuer_nodes=[]; belongs_issuer=[]; issued_by=[]
for issuer,g in sorted(issuer_to_group.items()):
    iuri=issuer_uri(issuer); name_esc=issuer.replace('"','\\"')
    issuer_nodes.append(f'{iuri} a fin:Company ;\n    rdfs:label "{name_esc}"@ko ;\n    fin:isListed "false"^^<http://www.w3.org/2001/XMLSchema#boolean> .')
    belongs_issuer.append(f"{iuri} fin:belongsToGroup {group_uri(g)} .")
    for bond_no in issuer_bonds[issuer]:
        issued_by.append(f"ex:Bond_{safe_local(bond_no)} fin:issuedBy {iuri} .")

htxt=f"@prefix fin: <{FIN}> .\n@prefix ex:  <{EX}> .\n@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n\n"
with open("/home/claude/out/instances_group.ttl","w",encoding="utf-8") as f:
    f.write(htxt)
    f.write("### 그룹 노드 ###\n"+"\n".join(group_nodes))
    f.write("\n\n### 주식회사 → 그룹 ###\n"+"\n".join(belongs_company))
    f.write("\n\n### 비상장 발행사 노드 ###\n"+"\n".join(issuer_nodes))
    f.write("\n\n### 발행사 → 그룹 ###\n"+"\n".join(belongs_issuer))
    f.write("\n\n### 채권 → 발행사 (issuedBy) ###\n"+"\n".join(issued_by)+"\n")

from collections import Counter
gb=Counter()
for issuer,g in issuer_to_group.items(): gb[g]+=len(issuer_bonds[issuer])
print("그룹 노드:",len(used_groups))
print("주식회사→그룹:",len(belongs_company))
print("신규 발행사 노드:",len(issuer_nodes))
print("발행사→그룹:",len(belongs_issuer))
print("채권→발행사(issuedBy):",len(issued_by))
print("\n그룹별 비상장발행사 채권 top:")
for g,c in gb.most_common(10): print(f"  {g}: {c}건")