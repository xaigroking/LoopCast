#!/usr/bin/env python3
"""Check permitted module edges and cycles, including deferred imports."""
import ast
from pathlib import Path

root = Path(__file__).resolve().parents[1]
package = root / "src/loopcast"
modules = {path.stem: path for path in package.glob("*.py")}
domain = {"domain", "contracts", "fixture", "generation", "ledger", "publisher", "runner", "storage", "worker"}
forbidden = {"fastapi", "starlette", "uvicorn", "next", "react"}
# Store/ledger are the persistence adapter; pure run rules live in domain.py.
allowed = {
    "domain": set(), "contracts": set(), "fixture": set(), "runner": set(),
    "migrations": set(), "diagnostics": set(), "publisher_cli": set(),
    "storage": {"domain", "ledger", "migrations", "studio_storage", "account_storage", "release_storage"}, "ledger": {"domain"},
    "mpt_bridge": set(), "mpt_install": {"contracts","media","network","runner"},
    "generator_diagnostics": {"contracts","generation","media","runner"},
    "studio_models": set(), "studio_rules": {"domain"},
    "studio_storage": {"domain", "studio_models", "studio_rules"},
    "studio_api": {"studio_models", "domain", "media", "generator_diagnostics"},
    "account_storage": {"domain"}, "release_storage": {"domain", "studio_rules"},
    "media": {"domain", "runner"}, "network": set(),
    "creation": {"domain", "generation", "media", "runner", "diagnostics"},
    "social": {"domain", "media", "network", "runner"}, "social_bridge": set(),
    "studio_service": {"domain", "runner", "diagnostics"},
    "generation": {"contracts", "runner", "diagnostics", "network"}, "worker": {"domain", "runner", "diagnostics"},
    "publisher": {"runner", "diagnostics"},
    "runtime": {"storage", "worker", "generation", "publisher", "media", "creation", "social", "studio_service"},
    "api": {"runtime", "storage", "diagnostics", "studio_api"}, "openapi": {"api"},
    "__main__": {"api", "diagnostics", "generator_diagnostics", "mpt_install"}, "__init__": set(),
}
graph = {name: set() for name in modules}
for name, path in modules.items():
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        imported = {item.name.split(".", 1)[0] for item in node.names} if isinstance(node, ast.Import) else ({node.module.split(".", 1)[0]} if isinstance(node, ast.ImportFrom) and node.module else set())
        if name in domain and forbidden.intersection(imported):
            raise SystemExit(f"Domain module {name} imports an HTTP/UI framework: {sorted(forbidden.intersection(imported))}")
        if name in {"domain", "contracts"} and imported.intersection({"sqlite3", "requests", "httpx", "urllib", "subprocess", "os", "pathlib"}):
            raise SystemExit(f"Pure domain module {name} imports I/O: {sorted(imported)}")
        if isinstance(node, ast.ImportFrom):
            if node.level == 1 and node.module:
                graph[name].add(node.module.split('.', 1)[0])
            elif node.level == 1:
                graph[name].update(alias.name for alias in node.names)
            elif node.module and node.module.startswith('loopcast.'):
                graph[name].add(node.module.split('.')[1])
        if isinstance(node, ast.Import):
            graph[name].update(alias.name.split('.')[1] for alias in node.names if alias.name.startswith('loopcast.'))
    if name not in allowed or graph[name] - allowed[name]:
        raise SystemExit(f"Forbidden module edges from {name}: {sorted(graph[name] - allowed.get(name, set()))}")

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
print(f"Architecture guards passed: {len(domain)} domain/adapter modules avoid transport/UI frameworks; {len(graph)} Python modules obey permitted edges and are acyclic, including deferred imports")
