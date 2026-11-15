"""成品抽检排查（纯函数）：在已提交配方/声明快照上定位漏报过敏原的缺声明原料。

输入：
- 指定过敏原（如“花生”）；
- 抽检结果：成品号 -> 阳性/阴性；
- 同一 state_version 的配方关系与原料声明。

判定规则：
1. 阳性成品若已知声明（整棵配方子树内任一原料的最高修订）含该过敏原，
   视为“已由声明解释”，不归咎于任何缺声明原料；
2. 阴性成品若已知声明含该过敏原，标记“与声明冲突”，且**不**把该阴性
   当作排除证据；
3. 其余阳性成品必须由若干“缺声明叶子原料”共同覆盖（每个阳性成品的
   配方子树至少含一种被选原料）；
4. 候选原料不得出现在任何有效的阴性成品中（冲突阴性不算有效排除证据）；
5. 在所有满足条件的组合中取原料数量最少者，并列出全部并列最小组合；
6. 无解时给出具体原因。本模块只读快照，绝不修改声明或标签。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from . import derive, graph

# 单次排查的规模上限（超出直接报错，避免组合爆炸与误操作）
MAX_CANDIDATES = 12
MAX_FINISHED_GOODS = 20


class InvestigationError(ValueError):
    """排查输入不满足前置条件（成品未知、超限等）。"""

    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


@dataclass
class Observations:
    finished_good_id: str
    result: str  # positive | negative
    classification: str  # explained_positive | unexplained_positive | conflict_negative | evidence_negative
    declared_sources: list[str]  # 已知声明中该过敏原的来源原料
    missing_leaves: list[str]  # 该成品子树内的缺声明叶子原料

    def to_dict(self) -> dict:
        return {
            "finished_good_id": self.finished_good_id,
            "result": self.result,
            "result_text": "阳性" if self.result == "positive" else "阴性",
            "classification": self.classification,
            "classification_text": _CLASSIFICATION_TEXT[self.classification],
            "declared_sources": self.declared_sources,
            "missing_leaves": self.missing_leaves,
        }


_CLASSIFICATION_TEXT = {
    "explained_positive": "阳性（已由已知声明解释）",
    "unexplained_positive": "阳性（已知声明无法解释，需排查）",
    "conflict_negative": "阴性（已知声明含该过敏原，结论冲突）",
    "evidence_negative": "阴性（有效排除证据）",
}


@dataclass
class Requirement:
    finished_good_id: str
    options: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"finished_good_id": self.finished_good_id, "options": self.options}


def _leaf_missing(result: derive.NodeResult, edges: dict[str, list[str]]) -> list[str]:
    """derive 的 missing 只会是叶子（无子项且无声明），这里再做一次显式过滤。"""
    return sorted(m for m in result.missing if m not in edges)


def _minimal_coverings(
    requirements: list[Requirement], active: list[str]
) -> list[list[str]]:
    """枚举全部并列最小命中组合。active 已排序，结果顺序稳定。"""
    if not requirements:
        return []
    cover = {
        req.finished_good_id: set(req.options) & set(active)
        for req in requirements
    }
    # 只剩空选项的阳性在调用前已判无解，这里再保险一次
    if any(not opts for opts in cover.values()):
        return []

    for size in range(1, len(active) + 1):
        found: list[list[str]] = []
        for combo in combinations(active, size):
            chosen = set(combo)
            if all(chosen & cover[req.finished_good_id] for req in requirements):
                found.append(list(combo))
        if found:
            return found
    return []


def analyze(
    edges: dict[str, list[str]],
    all_nodes: set[str],
    declarations: dict[str, dict[int, tuple[str, ...]]],
    sampling: dict[str, str],
    allergen: str,
) -> dict:
    """在给定快照上执行一次排查，返回可直接持久化/展示的结果。"""
    allergen = allergen.strip()
    if not allergen:
        raise InvestigationError("请指定要排查的过敏原")
    if not sampling:
        raise InvestigationError("抽检结果 CSV 没有任何有效数据行")

    if len(sampling) > MAX_FINISHED_GOODS:
        raise InvestigationError(
            f"单次排查最多涉及 {MAX_FINISHED_GOODS} 款成品，"
            f"本次抽检包含 {len(sampling)} 款，请拆分后重试"
        )

    roots = graph.finished_goods(edges)
    if not roots:
        raise InvestigationError("当前尚未上传有效配方关系，无法识别成品，请先上传配方")
    root_set = set(roots)
    unknown = sorted(fg for fg in sampling if fg not in root_set)
    if unknown:
        raise InvestigationError(
            "抽检结果中的成品号不是当前配方中的成品，未做任何排查",
            [f"未识别的成品号：{fg}" for fg in unknown],
        )

    memo = derive.derive_all(edges, all_nodes, declarations, roots=roots)

    observations: list[Observations] = []
    for fg, result in sampling.items():
        node = memo[fg]
        declared_sources = list(node.sources.get(allergen, []))
        has_declared = allergen in node.allergens
        missing_leaves = _leaf_missing(node, edges)
        if result == "positive":
            classification = (
                "explained_positive" if has_declared else "unexplained_positive"
            )
        else:
            classification = (
                "conflict_negative" if has_declared else "evidence_negative"
            )
        observations.append(
            Observations(
                finished_good_id=fg,
                result=result,
                classification=classification,
                declared_sources=declared_sources,
                missing_leaves=missing_leaves,
            )
        )

    positive_explained = [o for o in observations if o.classification == "explained_positive"]
    positive_unexplained = [
        o for o in observations if o.classification == "unexplained_positive"
    ]
    conflicts = [o for o in observations if o.classification == "conflict_negative"]
    negatives_evidence = [o for o in observations if o.classification == "evidence_negative"]

    # 候选原料 = 出现在“待解释阳性”配方子树中的缺声明叶子原料。
    # 已由声明解释的阳性不参与归咎，其叶子原料不应占用候选名额。
    universe: list[str] = []
    for obs in positive_unexplained:
        for material in obs.missing_leaves:
            if material not in universe:
                universe.append(material)
    universe.sort()
    if len(universe) > MAX_CANDIDATES:
        raise InvestigationError(
            f"单次排查最多涉及 {MAX_CANDIDATES} 个候选原料（缺声明叶子原料），"
            f"本次涉及 {len(universe)} 个，请缩小编成品范围后重试",
            ["涉及原料：" + "、".join(universe)],
        )

    # 有效阴性：阴性且不与已知声明冲突（冲突阴性的结果不可信，不作排除证据）
    excluded_by: dict[str, list[str]] = {}
    for obs in negatives_evidence:
        for material in obs.missing_leaves:
            excluded_by.setdefault(material, []).append(obs.finished_good_id)

    # 不在候选集合中的原料不展示为候选；阴性排除信息只对候选有意义
    excluded_by = {m: fgs for m, fgs in excluded_by.items() if m in universe}

    active = [m for m in universe if m not in excluded_by]
    candidate_dicts = []
    for material in universe:
        happens_in_positive = [
            o.finished_good_id for o in observations
            if o.result == "positive" and material in o.missing_leaves
        ]
        candidate_dicts.append(
            {
                "material_id": material,
                "in_positive_finished_goods": happens_in_positive,
                "excluded": material in excluded_by,
                "excluded_by_negative_finished_goods": excluded_by.get(material, []),
                "note": (
                    "出现在有效阴性成品中，已被排除，不参与组合"
                    if material in excluded_by
                    else "不在任何有效阴性成品中，可参与组合"
                ),
            }
        )

    reasons: list[str] = []
    requirements: list[Requirement] = []
    for obs in positive_unexplained:
        options = [m for m in obs.missing_leaves if m not in excluded_by]
        requirements.append(Requirement(obs.finished_good_id, options))
        if not options:
            if not obs.missing_leaves:
                reasons.append(
                    f"阳性成品 {obs.finished_good_id} 的配方子树内没有缺声明叶子原料，"
                    f"已知声明却无法解释“{allergen}”阳性：问题不在缺声明原料，"
                    "请核查声明或抽检本身"
                )
            else:
                excluded_desc = "、".join(
                    f"{m}（被阴性成品 {'、'.join(excluded_by[m])} 排除）"
                    for m in obs.missing_leaves
                )
                reasons.append(
                    f"阳性成品 {obs.finished_good_id} 的缺声明叶子原料"
                    f"（{excluded_desc}）全部出现在有效阴性成品中，"
                    "没有可用于解释该阳性的候选原料"
                )

    solutions: list[dict] = []
    minimum_size = 0
    if not positive_unexplained:
        if positive_explained:
            status = "no_blame"
            summary = (
                f"全部 {len(positive_explained)} 个阳性均可由已知声明解释，"
                "无需归咎于缺声明原料"
            )
        else:
            status = "no_positive"
            summary = "抽检结果中没有阳性成品，无需排查"
    elif reasons:
        status = "impossible"
        summary = "无解：存在无法由任何候选原料覆盖的阳性成品，详见原因"
    else:
        coverings = _minimal_coverings(requirements, active)
        if not coverings:
            status = "impossible"
            summary = "无解：找不到同时覆盖全部阳性且不在阴性成品中的原料组合"
            reasons.append(
                "候选原料无法同时覆盖全部待解释阳性成品，请补充抽检或核查声明"
            )
        else:
            status = "solved"
            minimum_size = len(coverings[0])
            for combo in coverings:
                chosen = set(combo)
                covers = {
                    req.finished_good_id: sorted(chosen & set(req.options))
                    for req in requirements
                }
                solutions.append({"ingredients": combo, "covers": covers})
            summary = (
                f"找到 {len(coverings)} 个并列最小组合，每个含 {minimum_size} 种"
                "缺声明叶子原料；仅为排查建议，未修改任何声明或标签"
            )

    return {
        "allergen": allergen,
        "status": status,
        "status_text": {
            "solved": "找到最小组合",
            "no_blame": "无需归咎缺声明原料",
            "no_positive": "没有阳性成品",
            "impossible": "无解",
        }[status],
        "summary": summary,
        "limits": {
            "max_candidates": MAX_CANDIDATES,
            "max_finished_goods": MAX_FINISHED_GOODS,
        },
        "sampling": [o.to_dict() for o in observations],
        "explained_positives": [
            {
                "finished_good_id": o.finished_good_id,
                "declared_sources": o.declared_sources,
            }
            for o in positive_explained
        ],
        "unexplained_positives": [
            o.finished_good_id for o in positive_unexplained
        ],
        "conflicts": [
            {
                "finished_good_id": o.finished_good_id,
                "declared_sources": o.declared_sources,
                "message": (
                    f"已知声明含“{allergen}”（来源："
                    f"{'、'.join(o.declared_sources) or '—'}），抽检却为阴性；"
                    "标记冲突，该阴性不作为排除证据"
                ),
            }
            for o in conflicts
        ],
        "negative_evidence": [o.finished_good_id for o in negatives_evidence],
        "candidates": candidate_dicts,
        "requirements": [r.to_dict() for r in requirements],
        "solutions": solutions,
        "solution_count": len(solutions),
        "minimum_size": minimum_size,
        "reasons": reasons,
        "note": "本排查仅基于当前已提交版本的配方与声明给出建议，未修改任何声明或标签",
    }
