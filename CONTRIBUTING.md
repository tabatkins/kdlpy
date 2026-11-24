Testing
=======

This library uses `pytest` for testing. 
If installed properly via `requirements-dev.txt`, just running `pytest` will execute all tests.
(This is also done automatically as part of the lint script.)
Run `pytest -h` or check the PyTest docs for help.

When prudent, add new tests either to one of the existing test scripts under `tests/`, 
or a new test script.

Do not alter the `upstream_tests` folder or its contents;
those come from the official KDL repository and represent the canonical testsuite.
They're tested by `tests/test_upstream.py`.

Coding Conventions
==================

Python Version & Dependencies
-----------------------------

* Target **Python 3.12+** (use modern features such as `from __future__ import annotations`, PEP 695 `type` aliases and generic functions, `@t.override`, `match`/`case`, and `collections.abc`).
* Keep the runtime library dependency-free (Python standard library only).

Naming
------

* Use `camelCase` for functions, methods, variables, parameters, and attributes/fields (e.g. `getEntry`, `nativeUntaggedValues`).
* Use `PascalCase` for classes and type aliases (e.g. `PrintConfig`, `NodeKey`).
* Use `UPPER_SNAKE_CASE` for module-level constants (e.g. `NATIVE_TAGS`, `_INITIAL_INDENT_DEPTH`).
* Prefix internal helpers, caches, and private types with a leading underscore (`_parseNodes`, `_PrintState`).
* Test functions under `tests/` use pytest's `test_snake_case` naming convention.

Style, Formatting & Linting
---------------------------

All code across the repository (`kdl/`, `tests/`, and root scripts) must pass `./lint` cleanly (`ruff`, `black`, `mypy`, `pylint` at 10/10, and `check-manifest`):

* **Formatting:** `black` with a **120-character** line length, and trailing commas on multiline calls/collections (`COM812`).
* **String concatenation:** Always use explicit `+` across lines rather than implicit multiline string concatenation (`ISC002`).
* **Exceptions:** Assign exception message strings/f-strings to a local variable (`msg = ...`) before raising (`EM101`/`EM102`), and chain causes explicitly (`from exc` / `from None`).
* **No `print()` in `kdl/`:** Library code must not call `print()` (`T201`); CLI I/O belongs in `kdl/cli.py`.
* **Imports:** Sorted via `ruff` (`I001`) with `combine-as-imports = true`. Public re-exports in `kdl/__init__.py` use explicit `X as X` aliases.
* **No drive-by lint disables:** Fix lint warnings properly rather than suppressing them, unless a narrow inline comment is genuinely unavoidable.

Typing & Patterns
-----------------

* **Full type annotations:** All functions and methods—including tests—must be fully typed (`mypy` runs with `disallow_untyped_defs`, `no_implicit_optional`, `warn_redundant_casts`, `warn_unused_ignores`, and `warn_return_any`).
* **Centralized typing module:** Keep shared type aliases and `TYPE_CHECKING` imports in `kdl/t.py` (imported as `from . import t`).
* **Lightweight objects:** Use `@dataclass(slots=True)`, `__slots__`, and `NamedTuple` for AST nodes, values, configs, and internal state.
* **Config defaults & sentinels:** Use chained `or` for optional config fallbacks (e.g. `config or self.printConfig or printing.defaults`), and `t.UNSET` when `None` is a valid distinct value.

Performance & Depth Safety
--------------------------

* Avoid unbounded recursion on nested document structures—use explicit stacks for parsing and printing so arbitrarily deep documents don't hit `RecursionError`.
* Keep hot paths lean: avoid `ABCMeta` on the `Value` hierarchy, keep `Stream.loc()` lazy (only computed on `ParseError`), and keep module-level caches lazily populated and bounded.
* Changes must stay within the call-count, memory, scaling, and speed budgets enforced by `check_perf.py` (run automatically by `./lint`).
