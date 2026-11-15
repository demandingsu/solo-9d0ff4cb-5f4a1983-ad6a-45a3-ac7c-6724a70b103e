"""按每种原料的最高修订推导成品过敏原与来源原料（纯函数）。

状态语义：
- ok    ：成品及其整棵配方子树用到的原料均有声明，可确定完整过敏原集合；
- pending：至少一个原料（含嵌套半成品）缺声明，必须标注“待核”，
           绝不能把缺声明当作“无过敏原”。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# declarations: material_id -> {revision -> tuple[allergens]}


@dataclass
class NodeResult:
    node_id: str
    status: str  # "ok" | "pending"
    allergens: list[str]
    # 过敏原 -> 来源原料号列表（去重、有序）
    sources: dict[str, list[str]]
    # 本节点子树中缺声明的原料号（去重、有序）
    missing: list[str]
    # 实际选用的原料修订号（仅含已声明原料）
    used_revision: dict[str, int] = field(default_factory=dict)


def _merge_sources(
    target: dict[str, list[str]], extra: dict[str, list[str]]
) -> None:
    for allergen, materials in extra.items():
        bucket = target.setdefault(allergen, [])
        for material in materials:
            if material not in bucket:
                bucket.append(material)


def derive_node(
    node: str,
    edges: dict[str, list[str]],
    declarations: dict[str, dict[int, tuple[str, ...]]],
    memo: dict[str, NodeResult],
    visiting: set[str],
) -> NodeResult:
    """后序遍历计算单个节点；环应在调用前已被 graph.assert_acyclic 拒收。"""
    if node in memo:
        return memo[node]
    if node in visiting:  # 防御性检查，正常不会走到
        raise ValueError(f"配方存在环：{node}")
    visiting.add(node)

    sources: dict[str, list[str]] = {}
    missing: set[str] = set()
    used_revision: dict[str, int] = {}

    # 1) 节点自身（若为原料且有声明）——按最高修订号取值
    rev_map = declarations.get(node)
    if rev_map:
        highest = max(rev_map)
        own_allergens = rev_map[highest]
        used_revision[node] = highest
        for allergen in own_allergens:
            sources.setdefault(allergen, [])
            if node not in sources[allergen]:
                sources[allergen].append(node)
    elif node not in edges:
        # 叶子原料（没有任何子项）且无声明 -> 缺声明，待核
        missing.add(node)
    # 节点既无声明又有子项：视为半成品，其过敏原完全由子项推导

    # 2) 递归合并所有直接子项（支持半成品复用、嵌套）
    for child in edges.get(node, []):
        child_result = derive_node(child, edges, declarations, memo, visiting)
        _merge_sources(sources, child_result.sources)
        missing.update(child_result.missing)
        used_revision.update(child_result.used_revision)

    visiting.discard(node)
    allergens = sorted(sources)
    result = NodeResult(
        node_id=node,
        status="pending" if missing else "ok",
        allergens=allergens,
        sources={name: sorted(sources[name]) for name in allergens},
        missing=sorted(missing),
        used_revision=used_revision,
    )
    memo[node] = result
    return result


def derive_all(
    edges: dict[str, list[str]],
    all_nodes: set[str],
    declarations: dict[str, dict[int, tuple[str, ...]]],
    roots: list[str] | None = None,
) -> dict[str, NodeResult]:
    """推导所有节点（默认全部节点；也可只算成品根节点）。"""
    memo: dict[str, NodeResult] = {}
    targets = roots if roots is not None else sorted(all_nodes)
    for node in targets:
        derive_node(node, edges, declarations, memo, set())
    return memo
