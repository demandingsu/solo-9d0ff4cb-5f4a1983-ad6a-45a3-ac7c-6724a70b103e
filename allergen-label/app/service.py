"""业务编排：上传校验 + 基于已提交快照的标注推导 + 抽检排查。"""
from __future__ import annotations

import json

from . import derive, graph, investigate
from .parse import (
    DeclarationBatch,
    ParseError,
    RelationBatch,
    parse_declarations,
    parse_relations,
    parse_sampling,
)
from .storage import Storage


class ServiceError(Exception):
    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


def upload_declarations(storage: Storage, data: bytes) -> dict:
    """校验并整版替换声明；任何校验失败都不触碰旧数据。"""
    try:
        batch: DeclarationBatch = parse_declarations(data)
    except ParseError as exc:
        raise ServiceError(exc.message, exc.details) from exc

    materials = len(batch.by_material)
    count = storage.replace_declarations(batch.rows)
    return {
        "accepted": True,
        "declaration_rows": count,
        "materials": materials,
        "message": f"声明已整版替换：{count} 行，覆盖 {materials} 种原料",
    }


def upload_relations(storage: Storage, data: bytes) -> dict:
    """解析 -> 环检测 -> 整版替换；有环则拒收整份文件、保留旧关系。"""
    try:
        batch: RelationBatch = parse_relations(data)
    except ParseError as exc:
        raise ServiceError(exc.message, exc.details) from exc

    cycle = graph.find_cycle(batch.edges)
    if cycle is not None:
        raise ServiceError(
            "配方存在环，已拒收整份关系文件，原有配方保持不变",
            ["环路径：" + " -> ".join(cycle)],
        )

    count = storage.replace_relations(batch.edges)
    roots = graph.finished_goods(batch.edges)
    return {
        "accepted": True,
        "relation_edges": count,
        "nodes": len(batch.all_nodes),
        "finished_goods": len(roots),
        "message": (
            f"配方关系已整版替换：{count} 条父子关系，"
            f"识别出 {len(roots)} 款成品"
        ),
    }


def _declaration_view(
    declarations: dict[str, dict[int, tuple[str, ...]]],
) -> list[dict]:
    view = []
    for material_id in sorted(declarations):
        rev_map = declarations[material_id]
        highest = max(rev_map)
        view.append(
            {
                "material_id": material_id,
                "revisions": sorted(rev_map),
                "highest_revision": highest,
                "allergens": list(rev_map[highest]),
            }
        )
    return view


def get_state(storage: Storage) -> dict:
    """读取同一已提交版本，推导成品标注，返回页面所需全部数据。"""
    state = storage.read_state()
    edges = state["edges"]
    all_nodes = state["all_nodes"]
    declarations = state["declarations"]
    roots = graph.finished_goods(edges)
    memo = derive.derive_all(edges, all_nodes, declarations, roots=roots)

    finished = []
    for fg in roots:
        r = memo[fg]
        finished.append(
            {
                "finished_good_id": fg,
                "status": r.status,
                "status_text": "待核" if r.status == "pending" else "已确定",
                "allergens": r.allergens,
                "sources": r.sources,
                "missing_declarations": r.missing,
                "used_revisions": r.used_revision,
            }
        )

    pending_count = sum(1 for f in finished if f["status"] == "pending")
    relation_view = [
        {"parent": parent, "child": child}
        for parent, kids in sorted(edges.items())
        for child in kids
    ]

    last = state["last_investigation"]
    investigation_view = None
    if last is not None:
        try:
            payload = json.loads(last["payload"])
        except (TypeError, ValueError):
            payload = None
        if payload is not None:
            investigation_view = {
                "id": last["id"],
                "created_at": last["created_at"],
                "analyzed_state_version": last["state_version"],
                "stale": last["state_version"] != state["version"],
                "stale_message": (
                    "配方或声明已更新，该排查结果已过期，请重新排查"
                    if last["state_version"] != state["version"]
                    else ""
                ),
                "result": payload,
            }

    return {
        "state_version": state["version"],
        "counts": {
            "materials": len(declarations),
            "relations": sum(len(v) for v in edges.values()),
            "nodes": len(all_nodes),
            "finished_goods": len(finished),
            "pending": pending_count,
        },
        "declarations": _declaration_view(declarations),
        "relations": relation_view,
        "finished_goods": finished,
        "snapshots": state["snapshots"],
        "investigation": investigation_view,
    }


def run_investigation(
    storage: Storage, allergen: str, data: bytes
) -> dict:
    """校验抽检 CSV，在读取到的同一已提交快照上排查并保存结果。

    排查只读快照，绝不修改声明/配方；保存结果不改变 state_version。
    """
    allergen = (allergen or "").strip()
    try:
        batch = parse_sampling(data)
    except ParseError as exc:
        raise ServiceError(exc.message, exc.details) from exc

    state = storage.read_state()
    try:
        result = investigate.analyze(
            edges=state["edges"],
            all_nodes=state["all_nodes"],
            declarations=state["declarations"],
            sampling=batch.by_finished_good,
            allergen=allergen,
        )
    except investigate.InvestigationError as exc:
        raise ServiceError(exc.message, exc.details) from exc

    saved = storage.save_investigation(
        allergen=allergen,
        state_version=state["version"],
        payload=json.dumps(result, ensure_ascii=False),
    )
    return {
        "accepted": True,
        "investigation_id": saved["id"],
        "created_at": saved["created_at"],
        "analyzed_state_version": state["version"],
        "state_version": state["version"],
        "stale": False,
        "result": result,
    }
