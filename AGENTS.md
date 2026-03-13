# Repository Coding Instructions

This repository follows the guidance in [`.codex/AGENTS.md`](C:/Code/docker_from_scratch_in_python/.codex/AGENTS.md).

Additional emphasis for this workspace:

- Prefer readable, explicit names over short or clever names.
- Important Python modules, classes, and methods should use Google-style docstrings.
- Complex code must include didactic comments that explain intent, invariants, and step-by-step flow.
- Infrastructure code that touches kernel features, syscalls, seccomp, namespaces, cgroups, mount setup, or bootstrap orchestration must be especially well commented.
- When a low-level probe relies on interpreting `errno` or another non-obvious signal, document why that signal means "supported", "unsupported", or "probe failed".
- When editing existing code, raise the documentation quality to match the most explanatory files in the project rather than preserving under-documented code.

The standard for this repository is that a human reader should be able to open a complex file and understand:

1. what it does
2. why it exists
3. how it maps to the spec
4. what happens step by step
