import copy
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb
from rdflib import Graph, Namespace, Literal
from rdflib.namespace import RDF, RDFS

from src.agent.agent_pipeline import FinancialAgent
from src.agent.query_builder import QueryBuilder
from src.agent.intent_validation import normalize_intent, IntentError
from src.agent.query_execution import verified_weight_unit, weight_filter

ROOT = Path(__file__).resolve().parents[1]
FIN = Namespace("http://mirae-agent.example/ontology#")
EX = Namespace("http://mirae-agent.example/data#")


def intent(scopes=None, **kwargs):
    return {"product_scopes": scopes or ["bond"], "graph_conditions": [], "sql_filters": [],
            "computed_filters": [], "limit": None, "sort": None,
            "temporal": {"kind": "snapshot", "start_date": None, "end_date": None}, **kwargs}


class FakeClova:
    def __init__(self, *responses):
        self.responses = responses
        self.calls = []

    def extract_intent(self, question, **kwargs):
        self.calls.append(kwargs)
        return copy.deepcopy(self.responses[min(len(self.calls) - 1, len(self.responses) - 1)])

    def generate_answer(self, *args):
        raise AssertionError("표시 개수·적용 조건은 LLM이 변경하면 안 됩니다.")


class QueryPlanningTests(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute("""CREATE TABLE bond(
            pd_no VARCHAR, pd_nm VARCHAR, std_pd_mcls_nm VARCHAR, srfc_irt DOUBLE,
            risk_grade_num INTEGER, mat_dt VARCHAR, bond_credit_grade VARCHAR,
            corp_pretax_yield DOUBLE, row_valid BOOLEAN, info_base_dt VARCHAR,
            bond_credit_rank INTEGER, remaining_days INTEGER)""")
        self.con.execute("""CREATE TABLE etf_domestic(
            pd_itm_no_ma VARCHAR, pd_nm VARCHAR, cu_fund_mgmt_co VARCHAR,
            cu_charge_rt DOUBLE, pd_risk_nm VARCHAR, du_last_aum DOUBLE,
            wu_inv_rgn VARCHAR, du_er_1y DOUBLE, row_valid BOOLEAN, pd_grp_no VARCHAR)""")
        self.graph = Graph()
        for name in ["신한은행", "삼성전자", "SK하이닉스"]:
            node = EX[f"Company_{name}"]
            self.graph.add((node, RDF.type, FIN.Company))
            self.graph.add((node, RDFS.label, Literal(name)))
        self.graph.add((EX.Group_신한, RDF.type, FIN.Group))
        self.graph.add((EX.Company_신한은행, FIN.belongsToGroup, EX.Group_신한))
        self.qb = QueryBuilder(ROOT / "data/dictionary/concepts.json", self.graph, self.con)

    def tearDown(self):
        self.con.close()

    def seed(self, n):
        for i in range(n):
            code = f"{i+1:06d}"
            self.con.execute("INSERT INTO bond VALUES (?, ?, '회사채', ?, 3, '20271231', 'AA-', 3, true, '20260821', 4, 365)",
                             ["KR" + code, f"채권{i+1}", i+1])
            self.con.execute("INSERT INTO etf_domestic VALUES (?, ?, '테스트', ?, '보통', 1000, '국내', 1, true, 'ETF')",
                             ["A" + code, f"ETF{i+1}", (i+1)/10])
            self.graph.add((EX[f"Bond_KR{code}"], FIN.issuedBy, EX.Company_신한은행))
            self.graph.add((EX[f"Product_{code}"], FIN.holds, EX.Company_신한은행))

    def agent(self, clova):
        return FinancialAgent(ROOT / "data/dictionary/concepts.json",
                              ROOT / "data/dictionary/unanswerable.json", None,
                              self.graph, self.con, clova)

    def test_default_zero_to_four_results(self):
        for n in range(5):
            with self.subTest(n=n):
                self.con.execute("DELETE FROM bond")
                self.con.execute("DELETE FROM etf_domestic")
                self.seed(n)
                result = self.qb.execute(intent())
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["returned_count"], min(n, 3))
                self.assertEqual(result["has_more"], n > 3)
                self.assertIn("LIMIT 4", result["sql"])
                response = self.agent(FakeClova(intent())).answer("채권 알려줘")
                self.assertEqual(response["status"], "no_result" if n == 0 else "ok")
                if n == 4:
                    self.assertIn("우선 3개", response["answer"])
                    self.assertNotIn("상품명=채권4", response["answer"])
                elif n:
                    self.assertIn(f"결과 {n}개", response["answer"])

    def test_explicit_limit_and_no_hidden_ten_row_truncation(self):
        self.seed(13)
        for count in [1, 5, 12]:
            response = self.agent(FakeClova(intent(limit=count))).answer(f"채권 {count}개")
            self.assertNotIn("우선", response["answer"])
            self.assertNotIn("추가 결과", response["answer"])
            self.assertNotIn("상품코드순", response["answer"])
            self.assertEqual(response["answer"].count("상품명=채권"), count)
        sorted_response = self.agent(FakeClova(intent(limit=1, sort={"concept": "표면금리", "order": "desc"}))).answer("금리가 가장 높은 채권")
        self.assertNotIn("우선", sorted_response["answer"])
        self.assertNotIn("순서부터", sorted_response["answer"])
        self.assertEqual(sorted_response["answer"].count("상품명=채권"), 1)
        shortage = self.agent(FakeClova(intent(limit=15))).answer("채권 15개")
        self.assertIn("15개보다 적은 13개", shortage["answer"])

    def test_multi_scope_three_each(self):
        self.seed(5)
        request = intent(["bond", "etf_domestic"], graph_conditions=[
            {"relation": "sameGroupAs", "target": "(주)신한은행", "direction": "reverse"}],
            purpose="group_exposure")
        result = self.qb.execute(request)
        self.assertEqual([r["returned_count"] for r in result["results"]], [3, 3])
        response = self.agent(FakeClova(request)).answer("같은 그룹의 채권이나 ETF")
        self.assertIn("상품군별로 최대 3개씩", response["answer"])
        self.assertNotIn("어느 상품군", response["answer"])
        self.assertEqual(response["answer"].count("상품명="), 6)

    def test_total_count_round_robin(self):
        self.seed(5)
        result = self.qb.execute(intent(["bond", "etf_domestic"], limit=5, limit_mode="total"))
        self.assertEqual([r["returned_count"] for r in result["results"]], [3, 2])
        self.assertTrue(all(r["has_more"] for r in result["results"]))
        response = self.agent(FakeClova(intent(["bond", "etf_domestic"], limit=1, limit_mode="total"))).answer("합쳐서 1개")
        self.assertIn("조회 가능한 결과는 있습니다", response["answer"])
        self.assertNotIn("일치하는 상품이 조회되지", response["answer"])

    def test_partial_support_does_not_drop_filter(self):
        self.seed(5)
        result = self.qb.execute(intent(["bond", "etf_domestic"],
                                       sql_filters=[{"concept": "총보수", "operator": "lte", "value": 0.2}]))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"][0]["status"], "unsupported")
        self.assertNotIn("sql", result["results"][0])
        self.assertEqual(result["results"][1]["returned_count"], 2)

    def test_missing_target_is_reinterpreted_once(self):
        self.seed(5)
        bad = intent(graph_conditions=[{"relation": "issuedBy", "target": None}])
        good = intent(graph_conditions=[{"relation": "sameGroupAs", "target": "신한은행"}])
        clova = FakeClova(bad, good)
        response = self.agent(clova).answer("신한은행과 같은 그룹 채권")
        self.assertEqual(response["status"], "ok")
        self.assertEqual(len(clova.calls), 2)
        self.assertIn("validation_feedback", clova.calls[1])
        self.assertNotIn("issuedBy(None)", response["think_trace"])
        repeated = FakeClova(bad)
        response = self.agent(repeated).answer("같은 그룹 채권")
        self.assertEqual(len(repeated.calls), 2)
        self.assertEqual(response["status"], "invalid_intent")
        self.assertNotIn("조건에 일치하는 상품이 조회되지", response["answer"])

    def test_unknown_filter_does_not_succeed_or_claim_applied(self):
        self.seed(5)
        request = intent(sql_filters=[{"concept": "합산비중", "operator": "lte", "value": 30}])
        response = self.agent(FakeClova(request)).answer("합산비중 30 이하")
        self.assertEqual(response["status"], "invalid_intent")
        self.assertNotIn("SQL 필터 적용", response["think_trace"])
        self.assertNotIn("상품명=", response["answer"])

    def test_group_missing_and_unknown_company_are_not_no_result(self):
        for target in ["삼성전자", "등록되지않은회사"]:
            result = self.qb.execute(intent(graph_conditions=[{"relation": "sameGroupAs", "target": target}]))
            self.assertEqual(result["status"], "data_missing")

    def test_group_search_is_not_special_cased_for_shinhan(self):
        self.seed(4)
        company, group = EX.Company_새로운회사, EX.Group_새로운그룹
        self.graph.add((company, RDF.type, FIN.Company))
        self.graph.add((company, RDFS.label, Literal("새로운회사")))
        self.graph.add((company, FIN.belongsToGroup, group))
        self.graph.add((group, RDF.type, FIN.Group))
        self.graph.add((EX.Bond_KR000004, FIN.issuedBy, company))
        result = self.qb.execute(intent(graph_conditions=[{"relation": "sameGroupAs", "target": "새로운회사"}]))
        self.assertEqual(result["data"]["product_code"].tolist(), ["KR000004"])

    def test_forward_holdings_default_three_and_intersection(self):
        self.seed(1)
        self.graph.add((EX.Theme_테스트, RDF.type, FIN.Theme))
        for i in range(4):
            company = EX[f"Company_Test{i}"]
            holding = EX[f"Holding_Test{i}"]
            self.graph.add((company, RDFS.label, Literal(f"회사{i}")))
            self.graph.add((EX.Product_000001, FIN.hasHolding, holding))
            self.graph.add((holding, FIN.ofCompany, company))
            self.graph.add((holding, FIN.weight, Literal(i+1)))
            self.graph.add((EX.Product_000001, FIN.holds, company))
        self.graph.add((EX.Product_000001, FIN.hasTheme, EX.Theme_테스트))
        self.graph.add((EX.Product_000002, FIN.hasTheme, EX.Theme_테스트))
        self.graph.add((EX.Product_000002, FIN.holds, EX.Company_Test0))
        result = self.qb.execute(intent(["etf_domestic"], graph_conditions=[
            {"relation": "holds", "target": "000001", "direction": "forward"}]))
        self.assertEqual(result["returned_count"], 3)
        self.assertTrue(result["has_more"])
        common = self.qb.execute(intent(["etf_domestic"], graph_conditions=[
            {"relation": "hasTheme", "target": "테스트"}], aggregation={"type": "intersection"}))
        self.assertEqual(common["data"], [("회사0", 2)])

    def test_ordering_precedes_limit(self):
        self.seed(5)
        result = self.qb.execute(intent(sort={"concept": "표면금리", "order": "desc"}))
        self.assertEqual(result["data"]["srfc_irt"].tolist(), [5, 4, 3])

    def test_computed_weights_before_limit_and_sort(self):
        self.seed(5)
        for i, total in enumerate([40, 35, 25, 20, 10], 1):
            product = EX[f"Product_{i:06d}"]
            for j, name in enumerate(["삼성전자", "SK하이닉스"]):
                node = EX[f"Company_{name}"]
                holding = EX[f"Holding_{i}_{j}"]
                self.graph.add((product, FIN.holds, node))
                self.graph.add((product, FIN.hasHolding, holding))
                self.graph.add((holding, FIN.ofCompany, node))
                self.graph.add((holding, FIN.weight, Literal(str(total / 2))))
        request = intent(["etf_domestic"], computed_filters=[{
            "type": "holding_weight_sum", "companies": ["삼성전자", "SK하이닉스", "에스케이하이닉스"],
            "operator": "lte", "value": 30, "unit": "percent"}],
            sort={"concept": "총보수", "order": "asc"}, limit=1)
        unverified = self.qb.execute(request)
        self.assertEqual(unverified["status"], "unsupported")
        self.assertNotIn("sql", unverified)
        with patch("src.agent.query_execution.verified_weight_unit", return_value="percent"):
            result = self.qb.execute(request)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["product_code"].tolist(), ["000003"])
        self.assertEqual(float(result["data"]["합산비중1(%)"].iloc[0]), 25)
        self.assertTrue(result["has_more"])
        self.graph.remove((EX.Holding_3_0, FIN.weight, None))
        with patch("src.agent.query_execution.verified_weight_unit", return_value="percent"):
            self.assertEqual(self.qb.execute(request)["status"], "data_error")

    def test_metadata_requires_exact_source_stamp(self):
        import json
        metadata = json.dumps({"weight_unit": "percent", "source_sha256": "abc", "evidence": "verified fixture"})
        with patch("src.agent.query_execution.Path.read_text", return_value=metadata):
            self.assertIsNone(verified_weight_unit(self.qb))
            self.graph.add((EX.HoldingsDataset, FIN.sourceSha256, Literal("abc")))
            self.assertEqual(verified_weight_unit(self.qb), "percent")
            self.graph.add((EX.HoldingsDataset, FIN.sourceSha256, Literal("old")))
            self.assertIsNone(verified_weight_unit(self.qb))

    def test_credit_grade_range_is_not_equality(self):
        self.seed(3)
        self.con.execute("UPDATE bond SET bond_credit_grade='AAA' WHERE pd_no='KR000001'")
        self.con.execute("UPDATE bond SET bond_credit_grade='A' WHERE pd_no='KR000003'")
        result = self.qb.execute(intent(sql_filters=[{"concept": "신용등급", "operator": "gte", "value": "AA-"}]))
        self.assertEqual(result["returned_count"], 2)

    def test_verified_precision_and_fraction_conversion(self):
        product = EX.Product_000001
        names = ["삼성전자", "SK하이닉스"]
        for i, (name, value) in enumerate(zip(names, ["20.01000022888184", "9.9899999"])):
            node = EX[f"Company_{name}"]
            holding = EX[f"Holding_precision{i}"]
            self.graph.add((product, FIN.holds, node))
            self.graph.add((product, FIN.hasHolding, holding))
            self.graph.add((holding, FIN.ofCompany, node))
            self.graph.add((holding, FIN.weight, Literal(value)))
        calc = {"companies": names, "operator": "lte", "value": 30}
        self.assertEqual(weight_filter(self.qb, calc, None, "percent", 2), {"000001": "30.00"})
        for i, value in enumerate(["0.20", "0.15"]):
            self.graph.set((EX[f"Holding_precision{i}"], FIN.weight, Literal(value)))
        self.assertEqual(weight_filter(self.qb, calc, None, "fraction"), {})
        calc["value"] = 35
        self.assertEqual(weight_filter(self.qb, calc, None, "fraction"), {"000001": "35.00"})

    def test_temporal_policy_is_applied_per_scope(self):
        self.seed(3)
        request = intent(["bond", "etf_domestic"], temporal={"kind": "observed", "start_date": "2026-08-21", "end_date": "2026-08-21"})
        response = self.agent(FakeClova(request)).answer("8월 21일 채권과 ETF")
        self.assertEqual(response["status"], "partial")
        self.assertIn("상품명=채권", response["answer"])
        self.assertIn("기준일이 모두 확인되지", response["answer"])

    def test_invalid_or_unimplemented_requirements_stop(self):
        for extra in [
            {"limit": 0}, {"graph_conditions": [{"relation": "unknown", "target": "X"}]},
            {"sql_filters": [{"concept": "총보수", "operator": "unknown", "value": 1}]},
            {"unsupported_conditions": ["계열사 발행 채권 제외"]},
            {"aggregation": {"type": "count"}},
            {"graph_conditions": [{"relation": ["holds"], "target": "X"}]},
            {"sort": {"concept": "총보수", "order": "asc", "period": []}},
            {"temporal": None},
        ]:
            with self.subTest(extra=extra):
                result = self.qb.execute(intent(**extra))
                self.assertEqual(result["type"], "problem")
                self.assertNotIn("sql", result)


if __name__ == "__main__":
    unittest.main()
