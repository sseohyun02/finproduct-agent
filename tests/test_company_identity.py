import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from openpyxl import Workbook
from rdflib import Graph, Namespace, Literal
from rdflib.namespace import RDF, RDFS

from src.graph.company_identity import CompanyRegistry, build_company_match_index
from src.graph.build_group import build_group_lines
from src.graph.build_ttl import gen_company_nodes, gen_holdings_and_issuance
from src.graph.resolve_company import build_stock_companies, match_bond_issuers
from src.agent.query_builder import QueryBuilder

ROOT = Path(__file__).resolve().parents[1]
FIN = Namespace("http://mirae-agent.example/ontology#")
EX = Namespace("http://mirae-agent.example/data#")
HEADER = f"""@prefix fin: <{FIN}> .
@prefix ex: <{EX}> .
@prefix rdfs: <{RDFS}> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""


def workbook(path, rows):
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    wb.save(path)
    wb.close()


class CompanyIdentityTests(unittest.TestCase):
    def setUp(self):
        self.registry = CompanyRegistry()

    def query(self, graph):
        return QueryBuilder(ROOT / "data/dictionary/concepts.json", graph=graph)

    def test_aliases_resolve_but_group_prefix_does_not(self):
        self.assertEqual(self.registry.identity("(주)신한은행")[0], "Company_신한은행")
        self.assertEqual(self.registry.match_key("에스케이하이닉스"),
                         self.registry.match_key("SK하이닉스"))
        self.assertIsNone(self.registry.lookup("신한이름만비슷한회사"))

    def test_group_unknown_still_has_issuer_and_duplicate_rows_are_deduplicated(self):
        rows = [("KR001", "(주)신한은행"), ("KR001", "신한은행"),
                ("KR002", "주식회사 신한은행"),
                ("KR003", "신한이름만비슷한회사"), ("KR004", "무소속회사")]
        ttl, review, stats = build_group_lines({}, rows)
        graph = Graph().parse(data=ttl, format="turtle")
        self.assertEqual(set(graph.subjects(FIN.issuedBy, EX.Company_신한은행)),
                         {EX.Bond_KR001, EX.Bond_KR002})
        self.assertIn((EX.Company_신한은행, FIN.belongsToGroup, EX.Group_신한), graph)
        self.assertNotIn((EX.Company_신한이름만비슷한회사, FIN.belongsToGroup, EX.Group_신한), graph)
        self.assertIn((EX.Bond_KR004, FIN.issuedBy, EX.Company_무소속회사), graph)
        self.assertEqual(stats["issuedBy"], 4)
        self.assertEqual(len(review), 2)
        self.assertFalse(list(graph.triples((None, FIN.isListed, None))))
        self.assertNotIn("Issuer_", ttl)
        qb = self.query(graph)
        for name in ["신한은행", "(주)신한은행", "주식회사 신한은행"]:
            self.assertEqual(qb.build_graph_query({"relation": "issuedBy", "target": name}),
                             {"KR001", "KR002"})
        self.assertEqual(qb.build_graph_query({"relation": "sameGroupAs", "target": "신한은행"}),
                         {"KR001", "KR002"})

    def test_registry_alias_collision_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "registry.json"
            path.write_text(json.dumps({"companies": [
                {"id": "Company_A", "name": "A", "aliases": ["공통"]},
                {"id": "Company_B", "name": "B", "aliases": ["(주)공통"]},
            ]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "충돌"):
                CompanyRegistry(path)

    def test_ambiguous_graph_company_is_not_silently_selected(self):
        graph = Graph()
        for node in [EX.Company_A, EX.Company_B]:
            graph.add((node, RDF.type, FIN.Company))
            graph.add((node, RDFS.label, Literal("같은이름")))
        with self.assertRaisesRegex(ValueError, "여러 회사"):
            self.query(graph).build_graph_query({"relation": "issuedBy", "target": "같은이름"})

    def test_excel_generation_and_reverse_holdings_share_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            portfolio = Path(temp) / "portfolio.xlsx"
            bonds = Path(temp) / "bonds.xlsx"
            workbook(portfolio, [
                ["etf", "ticker", "name", "a", "b", "c", "weight"],
                ["069500", "000660", "SK하이닉스", None, None, None, 3],
            ])
            workbook(bonds, [
                ["pd_no", "pd_nm", "pd_pbcm"],
                ["KR001", "다른이름1", "에스케이하이닉스"],
                ["KR001", "다른이름1", "(주)에스케이하이닉스"],
                ["KR002", "SK하이닉스2", None],
                ["KR003", "신한은행3", "(주)신한은행"],
            ])
            companies, codes = build_stock_companies(portfolio)
            self.assertEqual(companies["SK하이닉스"]["id"], "Company_SK하이닉스")
            matches, unmatched = match_bond_issuers(bonds, companies)
            self.assertEqual(matches["에스케이하이닉스"], "SK하이닉스")
            self.assertIn("(주)신한은행", unmatched)
            companies["SK하이닉스"]["bond_issuer_names"] = set(matches)
            holding, holds, issued, stats = gen_holdings_and_issuance(
                companies, codes, portfolio, bonds)
            graph = Graph().parse(data=HEADER + "\n".join(
                gen_company_nodes(companies) + holding + holds + issued), format="turtle")
            self.assertEqual(stats["bond_issued_rows"], 3)
            self.assertEqual(stats["bond_issued"], 2)
            qb = self.query(graph)
            self.assertEqual(qb.build_graph_query({"relation": "holds", "target": "에스케이하이닉스"}),
                             {"069500"})
            self.assertEqual(qb.build_graph_query({"relation": "issuedBy", "target": "SK하이닉스"}),
                             {"KR001", "KR002"})

    def test_existing_company_normalization_collision_fails(self):
        with self.assertRaisesRegex(ValueError, "충돌"):
            build_company_match_index({
                "ABC": {"name": "ABC"}, "abc": {"name": "abc"},
            })

    def test_explicit_alias_uses_canonical_id_and_name_in_generation_and_search(self):
        with tempfile.TemporaryDirectory() as temp:
            registry_path = Path(temp) / "registry.json"
            registry_path.write_text(json.dumps({"companies": [
                {"id": "Company_stable_id", "name": "대표회사",
                 "aliases": ["다른표기"], "group": None},
            ]}), encoding="utf-8")
            registry = CompanyRegistry(registry_path)
            portfolio = Path(temp) / "portfolio.xlsx"
            workbook(portfolio, [["etf", "ticker", "name"],
                                 ["069500", "000001", "다른표기"]])
            with patch("src.graph.resolve_company.CompanyRegistry", return_value=registry):
                companies, codes = build_stock_companies(portfolio)
            self.assertEqual(codes["000001"], "대표회사")
            self.assertEqual(companies["대표회사"]["id"], "Company_stable_id")
            ttl, _, _ = build_group_lines({}, [("KR001", "다른표기")], registry)
            graph = Graph().parse(data=ttl, format="turtle")
            qb = self.query(graph)
            qb.company_registry = registry
            self.assertEqual(qb.build_graph_query({"relation": "issuedBy", "target": "대표회사"}),
                             {"KR001"})

    def test_forward_holdings_aggregation_sort_and_limit_remain(self):
        import duckdb
        graph = Graph()
        graph.add((EX.Company_신한은행, RDF.type, FIN.Company))
        graph.add((EX.Company_신한은행, RDFS.label, Literal("신한은행")))
        graph.add((EX.Company_SK하이닉스, RDFS.label, Literal("SK하이닉스")))
        for number, company, weight in [(1, EX.Company_신한은행, "2"),
                                         (2, EX.Company_신한은행, "3"),
                                         (3, EX.Company_SK하이닉스, "4")]:
            holding = EX[f"Holding_{number}"]
            graph.add((EX.Product_069500, FIN.hasHolding, holding))
            graph.add((holding, FIN.ofCompany, company))
            graph.add((holding, FIN.weight, Literal(weight)))
        with duckdb.connect(":memory:") as con:
            con.execute("CREATE TABLE etf_domestic(pd_itm_no_ma VARCHAR, pd_nm VARCHAR, "
                        "row_valid BOOLEAN, pd_grp_no VARCHAR)")
            con.execute("INSERT INTO etf_domestic VALUES ('A069500', '테스트ETF', true, 'ETF')")
            qb = QueryBuilder(ROOT / "data/dictionary/concepts.json", graph=graph, duckdb_con=con)
            result = qb.execute({"product_scope": "etf_domestic", "limit": 1,
                                 "graph_conditions": [{"relation": "holds", "direction": "forward",
                                                       "target": "069500"}],
                                 "sort": {"concept": "보유비중", "order": "desc"}})
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["total_count"], 2)
            self.assertEqual(result["returned_count"], 1)
            self.assertEqual(result["data"][0]["weight"], "5")
            self.assertEqual(result["weight_unit"], "unverified")

    def test_temporal_reads_database_date_and_keeps_other_sources_unknown(self):
        import duckdb
        from src.agent.temporal_policy import TemporalPolicy
        with duckdb.connect(":memory:") as con:
            con.execute("CREATE TABLE bond(info_base_dt VARCHAR)")
            con.execute("INSERT INTO bond VALUES ('20260821')")
            policy = TemporalPolicy(con)
            self.assertEqual(policy.context()["bond"], "2026-08-21")
            self.assertIsNone(policy.context()["holdings"])
            self.assertIsNone(policy.check({"product_scope": "bond", "temporal": {
                "kind": "observed", "start_date": "2026-08-21", "end_date": "2026-08-21"}}))
            self.assertEqual(policy.check({"product_scope": "bond", "temporal": {
                "kind": "observed", "start_date": "2026-09-01", "end_date": "2026-09-01"}})[0],
                "unanswerable")


if __name__ == "__main__":
    unittest.main()
