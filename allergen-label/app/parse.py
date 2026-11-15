"""CSV 解析与字段校验（纯函数，不接触数据库）。

原料声明 CSV 列：原料号、修订号、过敏原
配方关系 CSV 列：父项、子项（每行一条 BOM 父子边）
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

DECLARATION_HEADERS = ["原料号", "修订号", "过敏原"]
RELATION_HEADERS = ["父项", "子项"]
# 同一单元格内多种过敏原的分隔符
_ALLERGEN_SPLIT = "，,；;、"


class ParseError(ValueError):
    """解析/校验失败，details 中保留全部行级错误。"""

    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


@dataclass(frozen=True)
class DeclarationRow:
    material_id: str
    revision: int
    allergens: tuple[str, ...]
    raw_allergen: str
    row_number: int


@dataclass
class DeclarationBatch:
    rows: list[DeclarationRow]
    # material_id -> {revision -> allergens}
    by_material: dict[str, dict[int, tuple[str, ...]]] = field(default_factory=dict)


@dataclass
class RelationBatch:
    # parent -> 去重后的有序子项列表
    edges: dict[str, list[str]]
    all_nodes: set[str]
    edge_count: int


def decode_bytes(data: bytes) -> str:
    """兼容带 BOM 的 UTF-8 与中文 Excel 常见的 GBK。"""
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParseError("文件编码无法识别，请保存为 UTF-8（可带 BOM）或 GBK 编码的 CSV")


def _split_allergens(raw: str) -> tuple[str, ...]:
    text = raw.strip()
    if not text:
        return ()
    for sep in _ALLERGEN_SPLIT:
        text = text.replace(sep, "\n")
    seen: set[str] = set()
    result: list[str] = []
    for part in text.split("\n"):
        name = part.strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


def _check_headers(fieldnames, expected: list[str]) -> None:
    if not fieldnames:
        raise ParseError("CSV 缺少表头，期望表头：" + "、".join(expected))
    actual = [h.strip() if h else "" for h in fieldnames]
    missing = [h for h in expected if h not in actual]
    unknown = [h for h in actual if h and h not in expected]
    problems: list[str] = []
    if missing:
        problems.append("缺少列：" + "、".join(missing))
    if unknown:
        problems.append("存在未知列：" + "、".join(unknown))
    if problems:
        raise ParseError(
            "表头不符合要求，期望列：" + "、".join(expected), problems
        )


def parse_declarations(data: bytes) -> DeclarationBatch:
    """解析原料声明 CSV。

    规则：
    - 修订号必须为非负整数；
    - 同号同修订重复出现且过敏原内容一致 -> 去重；
    - 同号同修订但内容不一致 -> 拒收（错误信息给出冲突行号）；
    - 过敏原单元格留空表示该修订声明“无过敏原”，与“缺声明”语义不同。
    """
    text = decode_bytes(data)
    reader = csv.DictReader(io.StringIO(text))
    _check_headers(reader.fieldnames, DECLARATION_HEADERS)

    errors: list[str] = []
    rows: list[DeclarationRow] = []
    # (material, revision) -> (row_number, allergens)
    seen_keys: dict[tuple[str, int], tuple[int, tuple[str, ...]]] = {}

    for row_index, raw_row in enumerate(reader, start=2):  # 第 1 行是表头
        if raw_row is None:
            continue
        # 跳过完全空白的行
        if not any((v or "").strip() for v in raw_row.values()):
            continue
        material_id = (raw_row.get("原料号") or "").strip()
        rev_text = (raw_row.get("修订号") or "").strip()
        allergen_text = (raw_row.get("过敏原") or "").strip()

        if not material_id:
            errors.append(f"第 {row_index} 行：原料号为空")
            continue
        try:
            revision = int(rev_text)
            if revision < 0:
                raise ValueError
        except ValueError:
            errors.append(
                f"第 {row_index} 行：修订号必须是非负整数，实际为 {rev_text!r}"
            )
            continue

        allergens = _split_allergens(allergen_text)
        key = (material_id, revision)
        if key in seen_keys:
            first_row, first_allergens = seen_keys[key]
            if first_allergens != allergens:
                errors.append(
                    f"第 {row_index} 行：原料 {material_id} 修订 {revision} "
                    f"与第 {first_row} 行内容不一致（同号同修订不允许异内容）"
                )
            continue  # 完全重复的行去重
        seen_keys[key] = (row_index, allergens)
        rows.append(
            DeclarationRow(
                material_id=material_id,
                revision=revision,
                allergens=allergens,
                raw_allergen=allergen_text,
                row_number=row_index,
            )
        )

    if errors:
        raise ParseError("原料声明校验失败", errors)
    if not rows:
        raise ParseError("原料声明 CSV 没有任何有效数据行")

    by_material: dict[str, dict[int, tuple[str, ...]]] = {}
    for row in rows:
        by_material.setdefault(row.material_id, {})[row.revision] = row.allergens

    return DeclarationBatch(rows=rows, by_material=by_material)


def parse_relations(data: bytes) -> RelationBatch:
    """解析配方关系 CSV 为父 -> 子的邻接表。"""
    text = decode_bytes(data)
    reader = csv.DictReader(io.StringIO(text))
    _check_headers(reader.fieldnames, RELATION_HEADERS)

    errors: list[str] = []
    edges: dict[str, list[str]] = {}
    edge_pairs: set[tuple[str, str]] = set()
    all_nodes: set[str] = set()
    edge_count = 0

    for row_index, raw_row in enumerate(reader, start=2):
        if raw_row is None:
            continue
        if not any((v or "").strip() for v in raw_row.values()):
            continue
        parent = (raw_row.get("父项") or "").strip()
        child = (raw_row.get("子项") or "").strip()
        if not parent or not child:
            errors.append(f"第 {row_index} 行：父项和子项均不能为空")
            continue
        all_nodes.add(parent)
        all_nodes.add(child)
        pair = (parent, child)
        if pair in edge_pairs:
            continue  # 完全重复的边去重
        edge_pairs.add(pair)
        edges.setdefault(parent, []).append(child)
        edge_count += 1

    if errors:
        raise ParseError("配方关系校验失败", errors)
    if edge_count == 0:
        raise ParseError("配方关系 CSV 没有任何有效数据行")

    return RelationBatch(edges=edges, all_nodes=all_nodes, edge_count=edge_count)
