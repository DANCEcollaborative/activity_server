"""
grading/grader/grader.py
════════════════════════
Central grading engine.  Called as a subprocess:

    python grader.py <notebook_path> <task_graders_path>

Output protocol (stdout):
    GRADER_SCORE:<float>
    GRADER_FEEDBACK_START
    <multi-line feedback text>
    GRADER_FEEDBACK_END

Using sentinels instead of line-position parsing means student code that
prints to stdout during exec cannot corrupt the result.

Grader discovery
────────────────
For each task tag found in the notebook (task1, task2, …) the engine looks for:

    <task_graders_path>/grade_task<N>.py

Each grade_task#.py must expose:

    def grade(student_solution) -> tuple[float, str | None]:

The parameter name convention controls what is passed:
    "code"      → raw source string
    "namespace" → the full exec'd variable dict
    anything else (e.g. "student_solution", "task1") →
                  that name looked up in the exec'd namespace and passed
                  as a callable
"""

import importlib.util
import inspect
import io
import os
import re
import sys
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from typing import Optional

import nbformat as nbf


# ──────────────────────────────────────────────
# Output sentinels
# ──────────────────────────────────────────────
SCORE_PREFIX   = "GRADER_SCORE:"
FEEDBACK_START = "GRADER_FEEDBACK_START"
FEEDBACK_END   = "GRADER_FEEDBACK_END"


# ──────────────────────────────────────────────
# Notebook helpers
# ──────────────────────────────────────────────

def _has_tag(cell, tag: str) -> bool:
    return tag in cell.get("metadata", {}).get("tags", [])


def _is_task_cell(cell, task_tag: str) -> bool:
    return cell.cell_type == "code" and _has_tag(cell, task_tag)


def extract_task_code(nb: nbf.NotebookNode, task_tag: str) -> str:
    """Concatenate source of all code cells tagged with *task_tag*."""
    return "\n".join(
        cell.source
        for cell in nb.cells
        if _is_task_cell(cell, task_tag)
    )


def discover_task_tags(nb: nbf.NotebookNode) -> list[str]:
    """Return task tags (task1, task2, …) found in the notebook, sorted numerically."""
    pattern = re.compile(r"^task\d+$")
    found: set[str] = set()
    for cell in nb.cells:
        for tag in cell.get("metadata", {}).get("tags", []):
            if pattern.match(tag):
                found.add(tag)
    return sorted(found, key=lambda t: int(t[4:]))


# ──────────────────────────────────────────────
# Grader loader
# ──────────────────────────────────────────────

def load_task_grader(task_graders_path: str, task_tag: str):
    """Dynamically load grade_task<N>.py. Returns the module or None."""
    n        = task_tag[4:]
    filepath = os.path.join(task_graders_path, f"grade_task{n}.py")
    if not os.path.isfile(filepath):
        return None
    spec   = importlib.util.spec_from_file_location(f"grade_task{n}", filepath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ──────────────────────────────────────────────
# Safe exec  (suppresses student stdout/stderr)
# ──────────────────────────────────────────────

def safe_exec(code: str, tag: str) -> tuple[dict, Optional[str]]:
    """
    exec() student code with stdout/stderr suppressed so stray print()
    calls cannot corrupt the grader's output channel.

    Returns (namespace, error_message_or_None).
    """
    namespace: dict = {}
    sink = io.StringIO()
    try:
        with redirect_stdout(sink), redirect_stderr(sink):
            exec(compile(code, f"<{tag}>", "exec"), namespace)  # noqa: S102
        return namespace, None
    except Exception as exc:
        return namespace, str(exc)


# ──────────────────────────────────────────────
# Core grading logic
# ──────────────────────────────────────────────

def grade_notebook(nb_path: str, task_graders_path: str) -> tuple[float, str]:
    """
    Grade a single notebook.  Returns (total_score, feedback_text).
    """
    with open(nb_path) as f:
        nb = nbf.read(f, as_version=4)

    task_tags = discover_task_tags(nb)

    header = [
        "=" * 60,
        "  GRADING REPORT",
        f"  Notebook : {os.path.basename(nb_path)}",
        f"  Graded   : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "=" * 60,
        "",
    ]

    if not task_tags:
        body = ["No task-tagged cells found in this notebook.", "",
                "Total Score: 0.0"]
        return 0.0, "\n".join(header + body)

    total_score = 0.0
    task_results: list[tuple[str, float, Optional[str]]] = []

    for tag in task_tags:
        code   = extract_task_code(nb, tag)
        module = load_task_grader(task_graders_path, tag)

        if module is None:
            task_results.append((tag, 0.0, f"No grader found for {tag}."))
            continue

        if not hasattr(module, "grade"):
            task_results.append((tag, 0.0,
                f"grade_task file for {tag} has no 'grade' function."))
            continue

        try:
            # Exec student code with stdout suppressed
            student_ns, exec_error = safe_exec(code, tag)
            if exec_error:
                task_results.append((tag, 0.0,
                    f"Student code failed to execute: {exec_error}"))
                continue

            # Resolve argument via grade()'s parameter name convention
            sig   = inspect.signature(module.grade)
            param = list(sig.parameters.values())[0]
            pname = param.name

            if pname == "code":
                arg = code
            elif pname == "namespace":
                arg = student_ns
            else:
                # Convention: the student defines def task<N>() which returns
                # their solution.  Pass the task<N> function itself to grade()
                # so grader files can call student_solution() to get the result.
                task_fn_name = tag   # e.g. "task1"
                task_fn = student_ns.get(task_fn_name)
                if task_fn is not None and callable(task_fn):
                    arg = task_fn
                else:
                    # Fallback: try the parameter name as a direct variable
                    arg = student_ns.get(pname)
                    if arg is None:
                        task_results.append((tag, 0.0,
                            f"Expected function '{task_fn_name}()' not found in student code."))
                        continue

            # Call grader with stdout suppressed too
            sink = io.StringIO()
            with redirect_stdout(sink), redirect_stderr(sink):
                result = module.grade(arg)

            if isinstance(result, (tuple, list)) and len(result) == 2:
                task_score, task_feedback = result
            else:
                task_score, task_feedback = float(result), None

            task_score = float(task_score)

        except Exception as exc:
            import traceback as _tb
            task_score    = 0.0
            task_feedback = f"Grader raised an exception: {exc}\n{_tb.format_exc()}"

        total_score += task_score
        task_results.append((tag, task_score, task_feedback))

    # Assemble feedback document
    body: list[str] = []
    for tag, score, fb in task_results:
        body.append(f"── {tag.upper()} {'─' * (50 - len(tag))}")
        body.append(f"   Score   : {score:.2f}")
        if fb:
            body.append(f"   Feedback: {fb}")
        body.append("")

    body.append("─" * 60)
    body.append(f"Total Score: {total_score:.2f}")
    body.append("=" * 60)

    return total_score, "\n".join(header + body)


# ──────────────────────────────────────────────
# Entry-point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: grader.py <notebook_path> <task_graders_path>", file=sys.stderr)
        sys.exit(1)

    nb_path           = sys.argv[1]
    task_graders_path = sys.argv[2]

    try:
        score, feedback = grade_notebook(nb_path, task_graders_path)
    except Exception as exc:
        import traceback
        print(f"{SCORE_PREFIX}0.0")
        print(FEEDBACK_START)
        print(f"Fatal grading error: {exc}\n{traceback.format_exc()}")
        print(FEEDBACK_END)
        sys.exit(1)

    print(f"{SCORE_PREFIX}{score}")
    print(FEEDBACK_START)
    print(feedback)
    print(FEEDBACK_END)
