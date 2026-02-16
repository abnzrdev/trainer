from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

BASE_DIR = "workspace"
TEMPLATE = "#include <iostream>\nint main() { ... }"


class WorkflowManager:
    def __init__(
        self,
        base_dir: str | Path = BASE_DIR,
        template: str = TEMPLATE,
        cached_samples: dict[str, Any] | None = None,
        cache_file: str | Path | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.template = template
        self.cached_samples = cached_samples
        self.cache_file = Path(cache_file) if cache_file is not None else self.base_dir / "samples_cache.json"

    def setup_workspace(self, contest: str, problem_id: str) -> Path:
        workspace_dir = self.base_dir / contest / problem_id
        workspace_dir.mkdir(parents=True, exist_ok=True)

        solution_path = workspace_dir / "solution.cpp"
        solution_path.write_text(self.template, encoding="utf-8")

        samples = self._get_cached_samples(contest, problem_id)
        if not samples:
            raise FileNotFoundError(
                f"No cached samples found for contest='{contest}', problem_id='{problem_id}'."
            )

        first_sample = samples[0]
        (workspace_dir / "test_1.in").write_text(first_sample.get("in", ""), encoding="utf-8")
        (workspace_dir / "test_1.out").write_text(first_sample.get("out", ""), encoding="utf-8")

        return workspace_dir

    @staticmethod
    def open_editor(file_path: str | Path) -> None:
        target = Path(file_path)
        subprocess.run(["nvim", str(target)], check=False)

    @staticmethod
    def run_tests(file_path: str | Path) -> tuple[bool, str]:
        source_path = Path(file_path)
        if not source_path.exists():
            return False, f"Source file not found: {source_path}"

        workspace_dir = source_path.parent
        binary_path = workspace_dir / "solution_bin"

        compile_proc = subprocess.run(
            ["g++", "-O2", str(source_path), "-o", str(binary_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if compile_proc.returncode != 0:
            return False, compile_proc.stderr or "Compilation failed."

        logs: list[str] = []
        all_passed = True

        input_files = sorted(workspace_dir.glob("*.in"))
        if not input_files:
            return False, "No .in test files found."

        for input_file in input_files:
            expected_file = input_file.with_suffix(".out")
            if not expected_file.exists():
                all_passed = False
                logs.append(f"Missing expected output file: {expected_file.name}")
                continue

            test_input = input_file.read_text(encoding="utf-8")
            expected_output = expected_file.read_text(encoding="utf-8")

            run_proc = subprocess.run(
                [str(binary_path)],
                input=test_input,
                capture_output=True,
                text=True,
                check=False,
            )

            if run_proc.returncode != 0:
                all_passed = False
                logs.append(
                    f"[{input_file.name}] Runtime error (code {run_proc.returncode}):\n{run_proc.stderr.strip()}"
                )
                continue

            actual = WorkflowManager._normalize_output(run_proc.stdout)
            expected = WorkflowManager._normalize_output(expected_output)
            if actual != expected:
                all_passed = False
                logs.append(
                    "\n".join(
                        [
                            f"[{input_file.name}] Wrong answer",
                            f"Expected:\n{expected_output.rstrip()}",
                            f"Got:\n{run_proc.stdout.rstrip()}",
                        ]
                    )
                )

        return all_passed, "\n\n".join(logs)

    def _get_cached_samples(self, contest: str, problem_id: str) -> list[dict[str, str]]:
        cache = self.cached_samples if self.cached_samples is not None else self._load_cache_file()

        if not isinstance(cache, dict):
            return []

        candidates: list[Any] = []

        if contest in cache and isinstance(cache[contest], dict):
            candidates.append(cache[contest].get(problem_id))

        candidates.append(cache.get(f"{contest}/{problem_id}"))

        for candidate in candidates:
            if isinstance(candidate, dict):
                samples = candidate.get("samples")
                if isinstance(samples, list):
                    return [s for s in samples if isinstance(s, dict)]
            if isinstance(candidate, list):
                return [s for s in candidate if isinstance(s, dict)]

        return []

    def _load_cache_file(self) -> dict[str, Any]:
        if not self.cache_file.exists():
            return {}

        with self.cache_file.open("r", encoding="utf-8") as cache_handle:
            payload = json.load(cache_handle)

        if isinstance(payload, dict):
            return payload

        return {}

    @staticmethod
    def _normalize_output(content: str) -> str:
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        return "\n".join(line.rstrip() for line in normalized.strip().split("\n"))
