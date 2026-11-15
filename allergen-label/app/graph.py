"""配方图（有向图）工具：环检测与遍历。"""
from __future__ import annotations

from collections.abc import Iterable


class CycleError(ValueError):
    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        arrow = " -> ".join(cycle)
        super().__init__(f"配方存在环，已拒收整份关系文件：{arrow}")


def find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    """三色 DFS 检测环；发现环时返回首尾相同的节点路径，如 [A, B, C, A]。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in edges}
    # 只作为子项出现的叶子节点不在 edges 中，遍历时自然终止
    stack: list[tuple[str, Iterable[str]]] = []

    for start in list(edges.keys()):
        if color.get(start, WHITE) != WHITE:
            continue
        color[start] = GRAY
        stack.append((start, iter(edges.get(start, []))))
        path = [start]
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                child_color = color.get(child, WHITE)
                if child_color == GRAY:
                    # 回边：从 path 中找到 child 的位置构成环
                    idx = path.index(child)
                    return path[idx:] + [child]
                if child_color == WHITE:
                    color[child] = GRAY
                    path.append(child)
                    stack.append((child, iter(edges.get(child, []))))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()
                path.pop()
    return None


def assert_acyclic(edges: dict[str, list[str]]) -> None:
    cycle = find_cycle(edges)
    if cycle is not None:
        raise CycleError(cycle)


def finished_goods(edges: dict[str, list[str]]) -> list[str]:
    """成品 = 作为父项出现、但从不作为任何项的子项（图的根节点）。"""
    parents = set(edges.keys())
    children: set[str] = set()
    for kids in edges.values():
        children.update(kids)
    return sorted(parents - children)
