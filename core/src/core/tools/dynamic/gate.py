"""生成代码的 AST 安全闸：只允许白名单 import，禁止危险调用/魔术属性。

这是"助手自己写工具"的第一道防线（第二道是双模型审查，第三道是冒烟测试+可卸载）。
"""
import ast

ALLOWED_IMPORTS = {
    "json", "re", "math", "datetime", "zoneinfo", "hashlib", "uuid", "httpx", "base64",
}
FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__", "open", "input",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "breakpoint", "exit", "quit", "help",
}
MAX_CODE_LENGTH = 20000


def check_generated_code(code: str) -> list[str]:
    """返回问题列表；空列表 = 通过。"""
    problems: list[str] = []
    if not code or not code.strip():
        return ["代码为空"]
    if len(code) > MAX_CODE_LENGTH:
        problems.append(f"代码过长（>{MAX_CODE_LENGTH} 字符）")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"语法错误: {e}"]

    has_handler = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "handler":
                has_handler = True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                if mod not in ALLOWED_IMPORTS:
                    problems.append(f"禁止 import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod not in ALLOWED_IMPORTS:
                problems.append(f"禁止 import: {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                problems.append(f"禁止调用: {func.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                problems.append(f"禁止访问魔术属性: {node.attr}")
    if not has_handler:
        problems.append("必须定义 handler 函数")
    return problems


def validate_spec(spec: dict) -> list[str]:
    """校验 LLM 生成的工具规格 JSON。"""
    problems = []
    name = str(spec.get("name") or "")
    import re as _re

    if not _re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name):
        problems.append("name 必须是 snake_case（小写字母开头，仅含小写/数字/下划线）")
    if not spec.get("description"):
        problems.append("缺少 description")
    params = spec.get("parameters")
    if not isinstance(params, dict) or params.get("type") != "object":
        problems.append("parameters 必须是 type=object 的 JSON Schema")
    if not isinstance(spec.get("code"), str) or not spec["code"].strip():
        problems.append("缺少 code")
    if not isinstance(spec.get("test_args"), dict):
        problems.append("test_args 必须是对象")
    return problems
