"""
内置工具：Calculator + Search
"""

from __future__ import annotations

import ast
import operator
import requests
from typing import Any, Dict

from .base import ToolDef, ToolRegistry


# ── 安全计算器 ────────────────────────────────────────────────────────────

_ALLOWED_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(expr: str) -> float:
    """
    安全计算数学表达式。
    只允许 +-*/() 和数字，禁止 __import__、os、eval 等危险操作。
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        raise ValueError(f"表达式语法错误: {expr}")

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
            return _ALLOWED_OPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.operand) in (ast.Add, ast.Sub):
            operand = _eval(node.operand)
            return -operand if isinstance(node.op, ast.USub) else operand
        raise ValueError(f"不支持的表达式: {ast.dump(node)}")

    return _eval(tree)


def calc_handler(**kwargs) -> str:
    """Calculator 工具处理函数"""
    expression = kwargs.get("expression", "")
    if not expression:
        return "错误：缺少 expression 参数"
    try:
        result = _safe_eval(expression)
        return str(result)
    except Exception as e:
        return f"计算失败: {e}"


CALC_TOOL = ToolDef(
    name="calc",
    description="计算数学表达式。支持 +, -, *, /, 括号。例如：'2 + 3 * 4'",
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "数学表达式，如 '2 + 3 * 4'",
            },
        },
        "required": ["expression"],
    },
    handler=calc_handler,
)


# ── 联网搜索（DuckDuckGo Instant Answer API）─────────────────────────────

def search_handler(**kwargs) -> str:
    """Search 工具处理函数（免费，无需 API Key）"""
    query = kwargs.get("query", "")
    if not query:
        return "错误：缺少 query 参数"
    try:
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        # 取 Instant Answer 或 AbstractText
        answer = data.get("Answer") or data.get("AbstractText") or ""
        if answer:
            return answer[:500]  # 截断，避免过长

        # 没有 instant answer，返回相关主题列表
        related = [r["Text"] for r in data.get("RelatedTopics", [])[:3]]
        if related:
            return "\n".join(related)

        return f"未找到「{query}」的相关结果"
    except Exception as e:
        return f"搜索失败: {e}"


SEARCH_TOOL = ToolDef(
    name="search",
    description="联网搜索。输入查询词，返回搜索结果摘要。例如：'北京天气'",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索查询词，如 '北京天气' 或 'Python 最新版本'",
            },
        },
        "required": ["query"],
    },
    handler=search_handler,
)


# ── 注册入口 ──────────────────────────────────────────────────────────────

def register_builtin_tools(registry: ToolRegistry):
    """将内置工具注册到指定注册表"""
    registry.register(CALC_TOOL)
    registry.register(SEARCH_TOOL)
