import ast
from conftest import REPO


def _tool_decorators():
    tree = ast.parse(next((REPO / "src").glob("*/server.py")).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for d in node.decorator_list:
                target = d.func if isinstance(d, ast.Call) else d
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    yield node.name, d


def test_every_tool_declares_annotations_with_read_only_hint():
    seen = 0
    for name, deco in _tool_decorators():
        seen += 1
        assert isinstance(deco, ast.Call), f"{name}: bare @mcp.tool, annotations missing"
        ann = {k.arg: k.value for k in deco.keywords}.get("annotations")
        assert isinstance(ann, ast.Call) and getattr(ann.func, "id", "") == "ToolAnnotations", f"{name}: annotations=ToolAnnotations(...) missing"
        kws = {k.arg: k.value for k in ann.keywords}
        assert "title" in kws and "readOnlyHint" in kws, f"{name}: title and readOnlyHint required"
        assert isinstance(kws["readOnlyHint"], ast.Constant) and isinstance(kws["readOnlyHint"].value, bool), f"{name}: readOnlyHint must be an explicit bool"
    assert seen >= 1
