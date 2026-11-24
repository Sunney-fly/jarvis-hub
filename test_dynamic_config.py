"""声明式配置工具测试：模板填充 / 路径提取 / YAML 加载。"""
import yaml

from core.tools.dynamic.config_tools import (
    build_config_tool,
    extract_path,
    fill_template,
    load_config_tools,
)


def test_fill_template_basic():
    assert fill_template({"a": "{x}", "b": ["{y}"]}, {"x": "1", "y": "2"}) == {"a": "1", "b": ["2"]}


def test_fill_template_missing_key_keeps_literal():
    assert fill_template("u/{q}", {}) == "u/{q}"


def test_extract_path_nested():
    data = {"data": {"results": [{"title": "t"}]}}
    assert extract_path(data, ".data.results.0.title") == "t"


def test_extract_path_dot_returns_whole():
    data = {"a": 1}
    assert extract_path(data, ".") == data
    assert extract_path(data, "") == data
    assert extract_path(data, None) == data


def test_extract_path_missing():
    assert extract_path({"a": 1}, ".b.c") is None


def test_build_config_tool_http():
    spec = {
        "name": "demo_api",
        "description": "演示",
        "parameters": {"type": "object", "properties": {}},
        "kind": "http",
        "http": {"url": "https://api.example.com/x", "response_path": "."},
    }
    tool = build_config_tool(spec, "test.yaml")
    assert tool is not None
    assert tool.name == "demo_api"
    assert tool.source == "config"


def test_build_config_tool_invalid_kind():
    tool = build_config_tool(
        {"name": "x", "description": "d", "parameters": {"type": "object"}, "kind": "nope"}, "t.yaml"
    )
    assert tool is None


def test_load_config_tools_from_dir(tmp_path):
    good = tmp_path / "good.yaml"
    good.write_text(
        yaml.safe_dump(
            {
                "name": "hello_cfg",
                "description": "配置工具",
                "parameters": {"type": "object", "properties": {}},
                "kind": "http",
                "http": {"url": "https://api.example.com/"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (tmp_path / "bad.yaml").write_text("name: [broken", encoding="utf-8")
    tools = load_config_tools(str(tmp_path))
    names = [t.name for t in tools]
    assert "hello_cfg" in names


def test_load_config_tools_missing_dir(tmp_path):
    assert load_config_tools(str(tmp_path / "nope")) == []
