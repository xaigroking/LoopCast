#!/usr/bin/env python3
"""Guard the domain-to-HTTP dependency direction and Python import cycles."""
import ast
from pathlib import Path

root = Path(__file__).resolve().parents[1]
package = root / "src/loopcast"
modules = {path.stem: path for path in package.glob("*.py")}
domain = {"contracts", "fixture", "generation", "ledger", "publisher", "runner", "storage", "worker"}
forbidden = {"fastapi", "starlette", "uvicorn", "next", "react"}
graph = {name: set() for name in modules}
for name, path in modules.items():
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        imported = {item.name.split(".", 1)[0] for item in node.names} if isinstance(node, ast.Import) else ({node.module.split(".", 1)[0]} if isinstance(node, ast.ImportFrom) and node.module else set())
        if name in domain and forbidden.intersection(imported):
            raise SystemExit(f"Domain module {name} imports an HTTP/UI framework: {sorted(forbidden.intersection(imported))}")
    # Only module-load imports define Python initialization cycles. Function-local imports
    # remain subject to the transport prohibition above and are resolved at call time.
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module in modules:
            graph[name].add(node.module)
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None:
            graph[name].update(alias.name for alias in node.names if alias.name in modules)

active, visited = [], set()
def visit(name):
    if name in active:
        cycle = active[active.index(name):] + [name]
        raise SystemExit("Python import cycle: " + " -> ".join(cycle))
    if name in visited:
        return
    active.append(name)
    for dependency in sorted(graph[name]):
        visit(dependency)
    active.pop()
    visited.add(name)

for module in sorted(graph):
    visit(module)
print(f"Architecture guards passed: {len(domain)} domain modules avoid transport/UI frameworks; {len(graph)} Python modules are acyclic")
