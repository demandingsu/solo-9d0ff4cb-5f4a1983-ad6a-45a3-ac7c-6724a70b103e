"""端到端自测：解析、环检测、推导、上传规则、API 与并发快照一致性。

运行（从项目根目录）：
    python -m unittest discover -s tests -v
"""
import io
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import graph, service  # noqa: E402
from app.parse import (  # noqa: E402
    ParseError,
    parse_declarations,
    parse_relations,
    parse_sampling,
)
from app.storage import Storage  # noqa: E402

SAMPLES = ROOT / "samples"


def read_sample(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


class ParseTests(unittest.TestCase):
    def test_declaration_highest_revision_and_blank_means_none(self):
        batch = parse_declarations(read_sample("declarations.csv"))
        # M01 有两个修订，最高 rev2 = 麸质
        self.assertEqual(batch.by_material["M01"][2], ("麸质",))
        # M03 rev2 过敏原留空 -> 空元组（明确无过敏原），与缺声明区分
        self.assertEqual(batch.by_material["M03"][2], ())
        self.assertEqual(batch.by_material["M02"][1], ("乳",))

    def test_duplicate_same_content_is_deduplicated(self):
        data = "原料号,修订号,过敏原\nM01,1,乳\nM01,1,乳\n".encode()
        batch = parse_declarations(data)
        self.assertEqual(len(batch.rows), 1)

    def test_same_key_different_content_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            parse_declarations(read_sample("declarations_conflict.csv"))
        self.assertTrue(ctx.exception.details)
        self.assertIn("不一致", ctx.exception.details[0])

    def test_bad_revision_rejected_and_keeps_old_state(self):
        data = "原料号,修订号,过敏原\nM01,abc,乳\n".encode()
        with self.assertRaises(ParseError):
            parse_declarations(data)

    def test_multi_allergen_separators(self):
        data = "原料号,修订号,过敏原\nM01,1, 乳；花生、小麦 ,乳\n".encode("gbk")
        batch = parse_declarations(data)
        self.assertEqual(batch.by_material["M01"][1], ("乳", "花生", "小麦"))

    def test_bad_headers_rejected(self):
        with self.assertRaises(ParseError):
            parse_relations("父,子\nA,B\n".encode())

    def test_relation_parse_dedup_edges(self):
        batch = parse_relations(read_sample("relations.csv"))
        self.assertEqual(batch.edge_count, 10)
        self.assertIn("S1", batch.edges)
        # 叶子原料在 all_nodes 中
        self.assertIn("M01", batch.all_nodes)


class GraphTests(unittest.TestCase):
    def test_cycle_detected_and_rejected(self):
        batch = parse_relations(read_sample("relations_cycle.csv"))
        cycle = graph.find_cycle(batch.edges)
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle[0], cycle[-1])
        with self.assertRaises(graph.CycleError):
            graph.assert_acyclic(batch.edges)

    def test_self_loop_is_cycle(self):
        batch = parse_relations("父项,子项\nA,A\n".encode())
        self.assertIsNotNone(graph.find_cycle(batch.edges))

    def test_acyclic_nested_graph(self):
        batch = parse_relations(read_sample("relations.csv"))
        self.assertIsNone(graph.find_cycle(batch.edges))
        roots = graph.finished_goods(batch.edges)
        self.assertEqual(roots, ["FG01", "FG02", "FG03"])


class DeriveTests(unittest.TestCase):
    def setUp(self):
        self.decls = parse_declarations(read_sample("declarations.csv")).by_material
        self.rel = parse_relations(read_sample("relations.csv"))

    def test_finished_good_allergens_and_sources(self):
        state = self._build_service_state()
        fg01 = next(f for f in state["finished_goods"] if f["finished_good_id"] == "FG01")
        # FG01 -> S2 -> S1(M01麸质,M02乳,M04蛋), M03(最高 rev2 显式无), M06(无)
        self.assertEqual(fg01["status"], "ok")
        # 按 Unicode 码位排序：乳(U+4E73) < 蛋(U+86CB) < 麸(U+9EA8)
        self.assertEqual(fg01["allergens"], ["乳", "蛋", "麸质"])
        self.assertEqual(fg01["sources"]["麸质"], ["M01"])
        self.assertEqual(fg01["sources"]["乳"], ["M02"])
        # M03 选用最高修订 rev2（无过敏原）
        self.assertEqual(fg01["used_revisions"]["M03"], 2)

    def test_missing_declaration_is_pending_not_none(self):
        # 未上传 M05 声明：FG02 含 M05 -> 待核，不能当作无过敏原
        state = self._build_service_state()
        fg02 = next(f for f in state["finished_goods"] if f["finished_good_id"] == "FG02")
        self.assertEqual(fg02["status"], "pending")
        self.assertIn("M05", fg02["missing_declarations"])
        # M03 最高修订 rev2 显式声明无过敏原，取代 rev1 的大豆：
        # 已确认过敏原为空，但缺声明的 M05 使其必须待核，不能当作整体无过敏原
        self.assertEqual(fg02["allergens"], [])

    def test_reused_semi_finished_nested(self):
        # S1 被 S2 复用；FG01 经两层嵌套仍能拿到 S1 内全部过敏原
        state = self._build_service_state()
        fg01 = next(f for f in state["finished_goods"] if f["finished_good_id"] == "FG01")
        self.assertEqual(fg01["sources"]["蛋"], ["M04"])

    def _build_service_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "t.db")
            storage.replace_declarations(
                parse_declarations(read_sample("declarations.csv")).rows
            )
            storage.replace_relations(self.rel.edges)
            return service.get_state(storage)


class ServiceRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_cycle_upload_rejected_and_old_relations_kept(self):
        service.upload_relations(self.storage, read_sample("relations.csv"))
        before = self.storage.read_state()
        with self.assertRaises(service.ServiceError) as ctx:
            service.upload_relations(self.storage, read_sample("relations_cycle.csv"))
        self.assertIn("环", ctx.exception.message)
        after = self.storage.read_state()
        self.assertEqual(after["edges"], before["edges"])
        self.assertEqual(after["version"], before["version"])

    def test_conflict_declaration_rejected_keeps_old(self):
        service.upload_declarations(self.storage, read_sample("declarations.csv"))
        before = self.storage.read_state()
        with self.assertRaises(service.ServiceError):
            service.upload_declarations(
                self.storage, read_sample("declarations_conflict.csv")
            )
        after = self.storage.read_state()
        self.assertEqual(after["declarations"], before["declarations"])

    def test_corrected_declaration_updates_affected_finished_goods(self):
        service.upload_declarations(self.storage, read_sample("declarations.csv"))
        service.upload_relations(self.storage, read_sample("relations.csv"))
        state1 = service.get_state(self.storage)
        fg02 = next(f for f in state1["finished_goods"] if f["finished_good_id"] == "FG02")
        self.assertEqual(fg02["status"], "pending")

        # 更正声明：补齐 M05 -> FG02 由待核变已确定，且带上坚果
        service.upload_declarations(
            self.storage, read_sample("declarations_corrected.csv")
        )
        state2 = service.get_state(self.storage)
        self.assertEqual(state2["state_version"], state1["state_version"] + 1)
        fg02 = next(f for f in state2["finished_goods"] if f["finished_good_id"] == "FG02")
        self.assertEqual(fg02["status"], "ok")
        self.assertIn("坚果", fg02["allergens"])
        self.assertEqual(fg02["sources"]["坚果"], ["M05"])
        # 其他成品不受影响
        fg01 = next(f for f in state2["finished_goods"] if f["finished_good_id"] == "FG01")
        self.assertEqual(fg01["status"], "ok")

    def test_revision_supersedes_old_allergen(self):
        # 新声明把 M01 增加 rev3 改为含花生 -> 最高修订生效
        data = (
            "原料号,修订号,过敏原\n"
            "M01,1,麸质\nM01,2,麸质\nM01,3,麸质;花生\n"
            "M02,1,乳\nM03,1,大豆\nM03,2,\nM04,1,蛋\nM06,1,\n"
        ).encode()
        service.upload_declarations(self.storage, data)
        service.upload_relations(self.storage, read_sample("relations.csv"))
        state = service.get_state(self.storage)
        fg01 = next(f for f in state["finished_goods"] if f["finished_good_id"] == "FG01")
        self.assertIn("花生", fg01["allergens"])
        self.assertEqual(fg01["used_revisions"]["M01"], 3)

    def test_full_replace_semantics(self):
        service.upload_relations(self.storage, read_sample("relations.csv"))
        # 用更小的新配方整版替换
        small = "父项,子项\nFG9,M01\n".encode()
        service.upload_relations(self.storage, small)
        state = service.get_state(self.storage)
        self.assertEqual(
            [f["finished_good_id"] for f in state["finished_goods"]], ["FG9"]
        )


class SamplingParseTests(unittest.TestCase):
    def test_results_normalized(self):
        batch = parse_sampling(
            "成品号,抽检结果\nFG1,阳性\nFG2,negative\nFG3,+\nFG4,阴\n".encode()
        )
        self.assertEqual(
            batch.by_finished_good,
            {"FG1": "positive", "FG2": "negative", "FG3": "positive", "FG4": "negative"},
        )

    def test_invalid_result_rejected(self):
        with self.assertRaises(ParseError):
            parse_sampling("成品号,抽检结果\nFG1,疑似\n".encode())

    def test_same_finished_good_conflicting_results_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            parse_sampling("成品号,抽检结果\nFG1,阳性\nFG1,阴性\n".encode())
        self.assertIn("不一致", ctx.exception.details[0])

    def test_bad_headers_rejected(self):
        with self.assertRaises(ParseError):
            parse_sampling("成品,结果\nFG1,阳性\n".encode())


class InvestigationTests(unittest.TestCase):
    """抽检排查：基于已提交快照，只读给出建议。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "t.db")
        service.upload_declarations(
            self.storage, read_sample("investigation_declarations.csv")
        )
        service.upload_relations(
            self.storage, read_sample("investigation_relations.csv")
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, name, allergen="花生"):
        return service.run_investigation(
            self.storage, allergen, read_sample(name)
        )["result"]

    def test_explained_positive_not_blamed_and_tied_minimum_listed(self):
        result = self._run("sampling.csv")
        self.assertEqual(result["status"], "solved")
        # FG10 由已声明 M10（花生）解释，不进入待解释阳性
        self.assertEqual(result["unexplained_positives"], ["FG11", "FG12"])
        self.assertEqual(
            [x["finished_good_id"] for x in result["explained_positives"]],
            ["FG10"],
        )
        # M05 出现在有效阴性 FG13，被排除；M03/M04 都覆盖 FG11+FG12，并列最小
        excluded = {c["material_id"]: c["excluded"] for c in result["candidates"]}
        self.assertTrue(excluded["M05"])
        self.assertFalse(excluded["M03"])
        self.assertEqual(
            [s["ingredients"] for s in result["solutions"]], [["M03"], ["M04"]]
        )
        self.assertEqual(result["minimum_size"], 1)
        self.assertEqual(result["solution_count"], 2)

    def test_conflict_negative_not_used_as_exclusion(self):
        # FG14 阴性但已知声明含花生（M10）-> 冲突，不得用于排除 M03
        result = self._run("sampling.csv")
        self.assertEqual(
            [c["finished_good_id"] for c in result["conflicts"]], ["FG14"]
        )
        self.assertNotIn("FG14", result["negative_evidence"])
        self.assertFalse(
            next(c for c in result["candidates"] if c["material_id"] == "M03")["excluded"]
        )
        # 同样含 M03 的 FG14 若按其阴性排除 M03，则组合会消失；此处仍应有解
        self.assertTrue(result["solutions"])

    def test_impossible_explains_reason(self):
        result = self._run("sampling_impossible.csv")
        self.assertEqual(result["status"], "impossible")
        self.assertEqual(result["solutions"], [])
        self.assertGreaterEqual(len(result["reasons"]), 2)
        joined = " ".join(result["reasons"])
        self.assertIn("FG11", joined)  # 候选全被阴性排除
        self.assertIn("FG15", joined)  # 无缺声明叶子，问题不在原料
        self.assertIn("FG16", joined)

    def test_all_positives_explained_needs_no_blame(self):
        result = self._run("sampling_no_blame.csv")
        self.assertEqual(result["status"], "no_blame")
        self.assertEqual(result["solutions"], [])

    def test_no_positive(self):
        result = self._run("sampling_no_positive.csv")
        self.assertEqual(result["status"], "no_positive")

    def test_unknown_finished_good_rejected(self):
        with self.assertRaises(service.ServiceError) as ctx:
            service.run_investigation(
                self.storage,
                "花生",
                "成品号,抽检结果\nFG99,阳性\n".encode(),
            )
        self.assertIn("FG99", " ".join(ctx.exception.details))

    def test_limits_rejected(self):
        # 20 款成品上限
        rows = "\n".join(f"FG{i},阴性" for i in range(21))
        with self.assertRaises(service.ServiceError) as ctx:
            self._run_raw("成品号,抽检结果\n" + rows + "\n")
        self.assertIn("20", ctx.exception.message)

        # 12 个候选原料上限：13 个缺声明叶子分别挂到 13 款成品
        rel = "父项,子项\n" + "\n".join(f"FGX{i},MX{i}" for i in range(13))
        service.upload_relations(self.storage, rel.encode())
        rows = "\n".join(f"FGX{i},阳性" for i in range(13))
        with self.assertRaises(service.ServiceError) as ctx:
            self._run_raw("成品号,抽检结果\n" + rows + "\n")
        self.assertIn("12", ctx.exception.message)

    def test_does_not_modify_declarations_or_labels(self):
        before = service.get_state(self.storage)
        self._run("sampling.csv")
        after = service.get_state(self.storage)
        self.assertEqual(before["state_version"], after["state_version"])
        self.assertEqual(before["declarations"], after["declarations"])
        self.assertEqual(before["finished_goods"], after["finished_goods"])

    def test_result_marked_stale_after_declaration_update(self):
        self._run("sampling.csv")
        state = service.get_state(self.storage)
        self.assertFalse(state["investigation"]["stale"])
        self.assertEqual(
            state["investigation"]["analyzed_state_version"], state["state_version"]
        )

        service.upload_declarations(
            self.storage, read_sample("declarations_corrected.csv")
        )
        state = service.get_state(self.storage)
        self.assertTrue(state["investigation"]["stale"])
        self.assertIn("已过期", state["investigation"]["stale_message"])
        # 旧结果仍可追溯，但记录其分析版本
        self.assertEqual(
            state["investigation"]["analyzed_state_version"],
            state["state_version"] - 1,
        )

    def test_result_marked_stale_after_relation_update(self):
        self._run("sampling.csv")
        service.upload_relations(
            self.storage, read_sample("investigation_relations.csv")
        )
        state = service.get_state(self.storage)
        self.assertTrue(state["investigation"]["stale"])

    def _run_raw(self, data):
        return service.run_investigation(self.storage, "花生", data.encode())


class InvestigationCoverTests(unittest.TestCase):
    """组合求解：最小规模 > 1 时仍列出全部并列最小组合。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "t.db")
        service.upload_declarations(
            self.storage,
            "原料号,修订号,过敏原\nD1,1,花生\n".encode(),
        )
        # FG1 -> A,B；FG2 -> C,D；FG3 -> D1（已声明，阳性由声明解释）
        service.upload_relations(
            self.storage,
            (
                "父项,子项\n"
                "FG1,A\nFG1,B\n"
                "FG2,C\nFG2,D\n"
                "FG3,D1\n"
            ).encode(),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_tied_minimum_pairs_listed(self):
        result = service.run_investigation(
            self.storage,
            "花生",
            "成品号,抽检结果\nFG1,阳性\nFG2,阳性\nFG3,阳性\n".encode(),
        )["result"]
        self.assertEqual(result["status"], "solved")
        self.assertEqual(result["minimum_size"], 2)
        self.assertEqual(result["solution_count"], 4)
        self.assertEqual(
            sorted(tuple(s["ingredients"]) for s in result["solutions"]),
            [("A", "C"), ("A", "D"), ("B", "C"), ("B", "D")],
        )
        # 每个组合都必须覆盖两个待解释阳性，且覆盖关系可追溯
        for solution in result["solutions"]:
            self.assertEqual(set(solution["covers"]), {"FG1", "FG2"})
            self.assertTrue(all(solution["covers"].values()))

    def test_choice_explained_by_declaration_is_ignored(self):
        result = service.run_investigation(
            self.storage,
            "花生",
            "成品号,抽检结果\nFG3,阳性\n".encode(),
        )["result"]
        self.assertEqual(result["status"], "no_blame")
        self.assertEqual(result["solutions"], [])


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["ALLERGEN_DB"] = str(Path(cls.tmp.name) / "api.db")
        # 延迟导入，确保环境变量生效
        from fastapi.testclient import TestClient

        from app.main import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _upload(self, endpoint, name):
        return self.client.post(
            f"/api/{endpoint}",
            files={"file": (name, read_sample(name), "text/csv")},
        )

    def test_01_full_flow_and_snapshot_consistency(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)

        r = self._upload("declarations", "declarations.csv")
        self.assertEqual(r.status_code, 200, r.text)
        r = self._upload("relations", "relations.csv")
        self.assertEqual(r.status_code, 200, r.text)

        state = self.client.get("/api/state").json()
        self.assertEqual(state["counts"]["finished_goods"], 3)
        self.assertEqual(state["counts"]["pending"], 1)  # FG02 缺 M05 声明
        # 同版本内声明与标注一致（M01 两行修订，共 5 种原料）
        self.assertEqual(len(state["declarations"]), 5)

    def test_02_conflict_returns_400(self):
        r = self._upload("declarations", "declarations_conflict.csv")
        self.assertEqual(r.status_code, 400)
        self.assertIn("不一致", r.json()["details"][0])

    def test_03_cycle_returns_400(self):
        r = self._upload("relations", "relations_cycle.csv")
        self.assertEqual(r.status_code, 400)
        self.assertIn("环", r.json()["error"])
        # 旧配方仍在
        state = self.client.get("/api/state").json()
        self.assertEqual(state["counts"]["finished_goods"], 3)

    def test_04_correction_clears_pending(self):
        r = self._upload("declarations", "declarations_corrected.csv")
        self.assertEqual(r.status_code, 200, r.text)
        state = self.client.get("/api/state").json()
        self.assertEqual(state["counts"]["pending"], 0)

    def test_05_concurrent_reads_see_committed_versions_only(self):
        versions_seen: list[int] = []
        errors: list[Exception] = []

        def reader():
            try:
                for _ in range(30):
                    data = self.client.get("/api/state").json()
                    v = data["state_version"]
                    # 同一响应内：成品标注引用的修订必须存在于同版本声明中
                    for fg in data["finished_goods"]:
                        for material, rev in fg["used_revisions"].items():
                            decl = next(
                                d
                                for d in data["declarations"]
                                if d["material_id"] == material
                            )
                            self.assertIn(rev, decl["revisions"])
                    versions_seen.append(v)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def writer():
            try:
                for i in range(5):
                    payload = (
                        "原料号,修订号,过敏原\n"
                        f"M01,{10 + i},麸质\nM02,1,乳\nM03,1,大豆\nM03,2,\n"
                        "M04,1,蛋\nM05,1,坚果\nM06,1,\n"
                    ).encode()
                    r = self.client.post(
                        "/api/declarations",
                        files={"file": ("d.csv", payload, "text/csv")},
                    )
                    self.assertEqual(r.status_code, 200, r.text)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=writer)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertFalse(errors, errors)
        self.assertTrue(versions_seen)

    def test_06_index_page(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("过敏原", r.text)

    def test_07_investigation_api_and_stale_flag(self):
        # 先保证有声明/配方
        self._upload("declarations", "investigation_declarations.csv")
        self._upload("relations", "investigation_relations.csv")
        r = self.client.post(
            "/api/investigations",
            data={"allergen": "花生"},
            files={
                "file": (
                    "sampling.csv",
                    read_sample("sampling.csv"),
                    "text/csv",
                )
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["result"]["status"], "solved")
        self.assertEqual(body["state_version"], body["analyzed_state_version"])

        state = self.client.get("/api/state").json()
        self.assertFalse(state["investigation"]["stale"])

        # 更新声明 -> 页面可据此标记旧结果过期
        self._upload("declarations", "declarations_corrected.csv")
        state = self.client.get("/api/state").json()
        self.assertTrue(state["investigation"]["stale"])

    def test_08_investigation_api_errors(self):
        r = self.client.post(
            "/api/investigations",
            data={"allergen": ""},
            files={"file": ("s.csv", b"x", "text/csv")},
        )
        self.assertEqual(r.status_code, 400)

        r = self.client.post(
            "/api/investigations",
            data={"allergen": "花生"},
            files={
                "file": (
                    "bad.csv",
                    "成品号,抽检结果\nFG99,阳性\n".encode(),
                    "text/csv",
                )
            },
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("FG99", " ".join(r.json()["details"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
