"""Structural guards on the architectural seam.

The plan for this project promises that ``collect.py`` is pure and that a future
Lambda runner can import it unchanged. A promise like that decays the moment
somebody adds a convenient ``os.environ`` read, and a code review will not
reliably catch it. These tests turn the promise into something that fails loudly.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "aistatus"

#: Modules that pull in ambient state or side effects. A pure module importing
#: any of these has leaked the seam.
FORBIDDEN_IMPORTS = {
    "os",
    "pathlib",
    "subprocess",
    "logging",
    "shutil",
    "tempfile",
    "sys",
    "runners",
}

#: Every module that must stay free of I/O. The adapters are included because a
#: single clock read inside one of them would churn the content hash on every
#: poll, which is the failure mode this whole design exists to prevent.
PURE_MODULES = [
    "collect.py",
    "models.py",
    "serialize.py",
    "diff.py",
    "plan.py",
    "adapters/__init__.py",
    "adapters/statuspage.py",
    "adapters/google_cloud.py",
]


def imported_names(path: pathlib.Path) -> set[str]:
    """Return the top-level module names a file imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Relative imports have no module root to check.
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("relative", PURE_MODULES)
def test_pure_modules_import_nothing_ambient(relative):
    leaked = imported_names(SRC / relative) & FORBIDDEN_IMPORTS
    assert not leaked, (
        f"{relative} imports {sorted(leaked)}, which breaks the purity seam. "
        "All filesystem, git, environment, and logging access belongs in runners/."
    )


#: Module roots whose ``.now()`` / ``.time()`` really do sample the wall clock.
CLOCK_ROOTS = {"datetime", "dt", "_dt", "time"}

#: Attribute names that sample the current instant.
CLOCK_ATTRS = {"now", "utcnow", "today", "time", "monotonic", "perf_counter"}


def _root_name(node: ast.AST) -> str | None:
    """Walk an attribute chain back to its root identifier."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


@pytest.mark.parametrize("relative", PURE_MODULES)
def test_pure_modules_never_sample_the_wall_clock(relative):
    """No pure module may call ``datetime.now()`` or ``time.time()``.

    Time enters this system only through the injected clock. A module that reads
    it directly produces snapshots that cannot be reproduced from their inputs,
    and — worse for this project — can churn the content hash on every poll.

    Reading an attribute that merely happens to be named ``now`` (such as
    ``ctx.now``, which holds an already-injected value) is fine; what matters is
    calling a real clock.
    """
    tree = ast.parse((SRC / relative).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in CLOCK_ATTRS:
            if _root_name(func) in CLOCK_ROOTS:
                pytest.fail(
                    f"{relative} calls {_root_name(func)}.{func.attr}(); time must "
                    "arrive via the injected clock so snapshots stay reproducible"
                )


def _strip_docstrings(source: str) -> str:
    """Return executable source with all docstrings removed."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def test_google_adapter_never_references_the_clock():
    """Google's derived component state must be a pure function of open incidents.

    A synthetic ``updated_at`` stamped with the current time would change the
    content hash on every poll, producing a commit every five minutes forever.
    Checked against executable code only, so the explanatory prose in the
    module's own docstrings does not trip it.
    """
    body = _strip_docstrings(
        (SRC / "adapters" / "google_cloud.py").read_text(encoding="utf-8")
    )
    assert "datetime" not in body
    assert "now()" not in body
    assert "time." not in body


def test_runner_is_the_only_place_with_git_or_filesystem_access():
    runner = pathlib.Path(__file__).resolve().parent.parent / "runners"
    runner_source = (runner / "github_actions.py").read_text(encoding="utf-8")
    # Sanity check in the opposite direction: the runner really does own the I/O,
    # so the guards above are constraining something real.
    assert "subprocess" in runner_source
    assert "Path" in runner_source
