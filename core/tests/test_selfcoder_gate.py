"""生成代码 AST 安全闸测试。"""
from core.tools.dynamic.gate import check_generated_code, validate_spec

CLEAN = (
    "import json\n"
    "async def handler(args, ctx):\n"
    "    return json.dumps({'ok': True}, ensure_ascii=False)\n"
)


def test_clean_code_passes():
    assert check_generated_code(CLEAN) == []


def test_reject_os_import():
    code = "import os\nasync def handler(args, ctx):\n    return 'x'\n"
    problems = check_generated_code(code)
    assert any("禁止 import: os" in p for p in problems)


def test_reject_import_from():
    code = "from subprocess import run\nasync def handler(args, ctx):\n    return 'x'\n"
    assert any("禁止 import" in p for p in check_generated_code(code))


def test_reject_eval():
    code = "async def handler(args, ctx):\n    return eval('1+1')\n"
    assert any("禁止调用: eval" in p for p in check_generated_code(code))


def test_reject_open():
    code = "async def handler(args, ctx):\n    open('/etc/passwd')\n    return 'x'\n"
    assert any("禁止调用: open" in p for p in check_generated_code(code))


def test_reject_dunder_attribute():
    code = "async def handler(args, ctx):\n    x = ().__class__\n    return 'x'\n"
    assert any("魔术属性" in p for p in check_generated_code(code))


def test_reject_missing_handler():
    assert any("必须定义 handler" in p for p in check_generated_code("x = 1\n"))


def test_reject_syntax_error():
    assert any("语法错误" in p for p in check_generated_code("def (:\n"))


def test_reject_empty():
    assert check_generated_code("") != []


def test_validate_spec_ok():
    spec = {
        "name": "hello_world",
        "description": "d",
        "parameters": {"type": "object", "properties": {}},
        "code": CLEAN,
        "test_args": {},
    }
    assert validate_spec(spec) == []


def test_validate_spec_bad_name():
    spec = {
        "name": "Bad-Name!",
        "description": "d",
        "parameters": {"type": "object", "properties": {}},
        "code": CLEAN,
        "test_args": {},
    }
    assert any("snake_case" in p for p in validate_spec(spec))
