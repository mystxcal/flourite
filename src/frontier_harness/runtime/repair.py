"""Codex-backed repair of one failed, replaceable runtime component.

The semantic run never becomes a repair prompt.  This module works only on an
isolated copy of the implementation that failed, and the stable supervisor
accepts the result only by binding a new immutable generation and replaying the
exact durable activity.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..config import RuntimePolicy
from ..util import canonical_json, sha256_text, utc_now
from .components import ComponentBinding, ComponentRegistry


@dataclass(frozen=True)
class ComponentFault:
    stage: str
    detail: str
    generation: int
    digest: str
    before_seq: int
    after_seq: int
    activity_key: str

    @property
    def fingerprint(self) -> str:
        return sha256_text(
            canonical_json(
                {
                    "stage": self.stage,
                    "detail": self.detail,
                    "activity_key": self.activity_key,
                }
            )
        )


class CodexComponentRepairer:
    """Create and bind a tested-by-replay component generation after a fault."""

    RECEIPTS_FILE = "repair-receipts.jsonl"

    def __init__(
        self,
        run_dir: Path,
        registry: ComponentRegistry,
        policy: RuntimePolicy,
    ) -> None:
        self.run_dir = run_dir.resolve()
        self.registry = registry
        self.policy = policy
        self.root = self.run_dir / "repairs"
        self.receipts = self.run_dir / self.RECEIPTS_FILE

    async def recover(self, binding: ComponentBinding, fault: ComponentFault) -> bool:
        if not self.policy.auto_repair:
            return False
        history = self._history()
        same_boundary = [
            item
            for item in history
            if item.get("fingerprint") == fault.fingerprint
        ]
        if any(
            item.get("outcome") == "retry_unchanged"
            and item.get("failed_digest") == binding.digest
            for item in same_boundary
        ):
            self._record(
                fault,
                outcome="stopped_no_progress",
                detail="the exact activity was replayed once against unchanged code",
            )
            return False

        attempt = len(same_boundary) + 1
        attempt_dir = self.root / fault.fingerprint[:16] / f"attempt-{attempt:02d}"
        while attempt_dir.exists():
            attempt += 1
            attempt_dir = self.root / fault.fingerprint[:16] / f"attempt-{attempt:02d}"
        source = attempt_dir / "source" / "frontier_harness"
        attempt_dir.mkdir(parents=True, exist_ok=False)
        shutil.copytree(self.registry.slot_path(binding) / "frontier_harness", source)
        (attempt_dir / "fault.json").write_text(
            json.dumps(
                {
                    "protocol": "flourite-component-repair/v1",
                    "fault": asdict(fault),
                    "fingerprint": fault.fingerprint,
                    "run_dir": str(self.run_dir),
                    "run_state": self._state_excerpt(),
                    "evidence": self._evidence_map(fault),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        prompt = self._prompt(attempt_dir)
        output = attempt_dir / "codex-last-message.txt"
        command = [
            self.policy.repair_command,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            self.policy.repair_model,
            "--config",
            f'model_reasoning_effort="{self.policy.repair_reasoning_effort}"',
            "--cd",
            str(attempt_dir / "source"),
            "--output-last-message",
            str(output),
            "-",
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")),
                timeout=self.policy.repair_timeout_seconds,
            )
        except TimeoutError as exc:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()
            self._record(fault, outcome="repairer_failed", detail=f"TimeoutError: {exc}")
            return False
        except FileNotFoundError as exc:
            self._record(fault, outcome="repairer_failed", detail=f"{type(exc).__name__}: {exc}")
            return False
        (attempt_dir / "codex-stdout.log").write_bytes(stdout)
        (attempt_dir / "codex-stderr.log").write_bytes(stderr)
        if process.returncode != 0:
            self._record(
                fault,
                outcome="repairer_failed",
                detail=f"Codex exited {process.returncode}",
            )
            return False

        try:
            repaired = self.registry.bind(attempt_dir / "source")
        except Exception as exc:  # the registry is the deterministic admission gate
            self._record(
                fault,
                outcome="candidate_rejected",
                detail=f"{type(exc).__name__}: {exc}",
            )
            return False
        if repaired.digest == binding.digest:
            # A no-change diagnosis gets one exact replay.  If the same fault
            # returns, the receipt makes the lack of progress decisive.
            prior_no_change = any(item.get("outcome") == "retry_unchanged" for item in same_boundary)
            outcome = "stopped_no_progress" if prior_no_change else "retry_unchanged"
            self._record(fault, outcome=outcome, detail="Codex made no component change")
            return not prior_no_change

        self._record(
            fault,
            outcome="applied",
            detail=f"bound generation {repaired.generation}",
            repaired_generation=repaired.generation,
            repaired_digest=repaired.digest,
        )
        return True

    def _state_excerpt(self) -> dict[str, Any]:
        try:
            state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return {
            key: state.get(key)
            for key in ("run_id", "status", "pause_kind", "terminal_reason", "last_event_seq")
        }

    def _evidence_map(self, fault: ComponentFault) -> dict[str, Any]:
        return {
            "state": str(self.run_dir / "state.json"),
            "ledger": str(self.run_dir / "ledger.sqlite3"),
            "components": str(self.run_dir / "components.json"),
            "component_receipts": str(self.run_dir / "component-receipts.jsonl"),
            "repair_receipts": str(self.receipts),
            "provider_sessions": str(self.run_dir / "provider-sessions"),
            "kernel_execution": str(self.run_dir / "kernel-executions" / fault.activity_key),
            "workspace_root": str(self.run_dir / "software" / "worktrees"),
        }

    def _history(self) -> list[dict[str, Any]]:
        try:
            lines = self.receipts.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        history: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                history.append(value)
        return history

    def _record(
        self,
        fault: ComponentFault,
        *,
        outcome: str,
        detail: str,
        repaired_generation: int | None = None,
        repaired_digest: str | None = None,
    ) -> None:
        payload = {
            "timestamp": utc_now(),
            "fingerprint": fault.fingerprint,
            "before_seq": fault.before_seq,
            "after_seq": fault.after_seq,
            "activity_key": fault.activity_key,
            "failed_generation": fault.generation,
            "failed_digest": fault.digest,
            "outcome": outcome,
            "detail": detail,
            "repaired_generation": repaired_generation,
            "repaired_digest": repaired_digest,
        }
        self.receipts.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.receipts, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, (canonical_json(payload) + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _prompt(attempt_dir: Path) -> str:
        return f"""You are Flourite's infrastructure repairer, not a task-solving agent.

Read `{attempt_dir / 'fault.json'}` and inspect the `frontier_harness` package in this
workspace. The fault contains a read-only map to the actual durable run, provider traces,
receipts, and failed activity. Use those freely to reconstruct what happened; do not rely on
the controller's summary or match an error template. Find the general implementation defect
that explains the exact fault. Fix the causing layer only. Do not hardcode the current task,
run id, path, artifact, or error text. Do not alter any durable run data. Preserve public
interfaces and semantic behavior.

Work directly and quickly. Run cheap compile/import or focused checks that discriminate your
hypothesis. The stable supervisor will make the final decision by atomically binding your
candidate and replaying the exact failed activity. If the fault is external/transient and no
code change is justified, leave the source unchanged and say so. Do not manufacture a patch.
"""
