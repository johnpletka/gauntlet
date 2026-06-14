"""Triage-accuracy evaluation (plan P4 assumption test, review F-009).

The P4 assumption is "cheap-model triage is accurate enough": ≥ 85% verdict
agreement with the hand-labeled bootstrap corpus AND zero blocking-severity
findings misclassified into a reject category *without escalation*, reported
as a per-severity confusion matrix — aggregate agreement alone can hide a
catastrophic blocking false-negative (review F-009).

The math lives here (unit-tested, offline); the live measurement is the
integration test, which runs the configured cheap model over the corpus with
the exact prompt the cycle ships (:func:`gauntlet.engine.cycle.triage_prompt`).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gauntlet.engine.cycle import REJECT_VERDICTS, needs_escalation

VERDICTS = ("legitimate", "bikeshedding", "premature_optimization", "not_applicable")
SEVERITIES = ("blocking", "major", "minor", "nit")


@dataclass
class CorpusEntry:
    id: str
    source: str
    context: str
    finding: dict[str, Any]
    label: dict[str, str]

    @property
    def severity(self) -> str:
        return self.finding.get("severity", "")


def load_corpus(path: Path) -> list[CorpusEntry]:
    entries = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        entries.append(CorpusEntry(**raw))
    return entries


@dataclass
class EvalReport:
    total: int = 0
    agreements: int = 0
    # per-severity confusion: {severity: Counter[(label_verdict, predicted_verdict)]}
    confusion: dict[str, Counter] = field(default_factory=dict)
    # blocking findings whose predicted verdict landed in a reject category
    # while the label did not — split by whether escalation caught them.
    blocking_misses_escalated: list[str] = field(default_factory=list)
    blocking_misses_unescalated: list[str] = field(default_factory=list)
    action_agreements: int = 0
    disagreements: list[dict[str, str]] = field(default_factory=list)

    @property
    def agreement(self) -> float:
        return self.agreements / self.total if self.total else 0.0

    @property
    def action_agreement(self) -> float:
        return self.action_agreements / self.total if self.total else 0.0

    def passes_exit_criteria(self) -> bool:
        """Plan P4 exit: ≥85% agreement AND zero unescalated blocking misses."""
        return self.agreement >= 0.85 and not self.blocking_misses_unescalated


def evaluate(
    entries: list[CorpusEntry], predictions: dict[str, dict[str, Any]]
) -> EvalReport:
    """Score model verdicts against the hand labels.

    ``predictions`` maps corpus-entry id → the model's verdict object
    (``verdict``/``action``/``confidence``). Escalation is judged with the
    shipped :func:`needs_escalation` rule, so the guarantee measured is the
    guarantee deployed.
    """
    report = EvalReport()
    for entry in entries:
        pred = predictions[entry.id]
        report.total += 1
        label_v = entry.label["verdict"]
        pred_v = pred.get("verdict", "")
        severity = entry.severity
        report.confusion.setdefault(severity, Counter())[(label_v, pred_v)] += 1
        if pred_v == label_v:
            report.agreements += 1
        else:
            report.disagreements.append({
                "id": entry.id, "severity": severity,
                "label": label_v, "predicted": pred_v,
                "confidence": pred.get("confidence", "?"),
                "reasoning": pred.get("reasoning", ""),
            })
        if pred.get("action") == entry.label.get("action"):
            report.action_agreements += 1
        if (
            severity == "blocking"
            and pred_v in REJECT_VERDICTS
            and label_v not in REJECT_VERDICTS
        ):
            if needs_escalation(severity, pred):
                report.blocking_misses_escalated.append(entry.id)
            else:  # structurally unreachable while the rule escalates all
                report.blocking_misses_unescalated.append(entry.id)  # blockers
    return report


def render_report(report: EvalReport, *, model: str, corpus_path: str) -> str:
    """Markdown report: the recorded artifact the P4 exit criteria ask for."""
    lines = [
        "# Triage accuracy — P4 assumption test (review F-009)",
        "",
        f"- model: `{model}`",
        f"- corpus: `{corpus_path}` ({report.total} hand-labeled findings)",
        f"- verdict agreement: **{report.agreement:.1%}** "
        f"({report.agreements}/{report.total}; exit ≥ 85%)",
        f"- action agreement (secondary): {report.action_agreement:.1%}",
        f"- blocking→reject misses without escalation: "
        f"**{len(report.blocking_misses_unescalated)}** (exit: zero)"
        + (f" — {report.blocking_misses_unescalated}"
           if report.blocking_misses_unescalated else ""),
        f"- blocking→reject misses caught by escalation: "
        f"{len(report.blocking_misses_escalated)}"
        + (f" — {report.blocking_misses_escalated}"
           if report.blocking_misses_escalated else ""),
        f"- exit criteria: {'**PASS**' if report.passes_exit_criteria() else '**FAIL**'}",
        "",
        "## Per-severity confusion matrices (label rows × predicted columns)",
    ]
    for severity in SEVERITIES:
        counter = report.confusion.get(severity)
        if not counter:
            continue
        n = sum(counter.values())
        lines += ["", f"### {severity} (n={n})", ""]
        lines.append("| label \\ predicted | " + " | ".join(VERDICTS) + " |")
        lines.append("|---|" + "---|" * len(VERDICTS))
        labels = sorted({lab for (lab, _p) in counter})
        for lab in labels:
            row = [str(counter.get((lab, p), 0)) for p in VERDICTS]
            lines.append(f"| {lab} | " + " | ".join(row) + " |")
    if report.disagreements:
        lines += ["", "## Disagreements", ""]
        for d in report.disagreements:
            lines.append(
                f"- `{d['id']}` ({d['severity']}): labeled **{d['label']}**, "
                f"model said **{d['predicted']}** "
                f"(confidence {d['confidence']}) — {d['reasoning']}"
            )
    lines += [
        "",
        "## Corpus caveat (recorded honestly)",
        "",
        "The corpus is harvested from the bootstrap's own plan/P1/P2/P3 review "
        "rounds, where almost every finding was triaged `legitimate` (34/36); "
        "`nit` severity never occurred. A constant-`legitimate` predictor would "
        "score ~94% — the aggregate gate is therefore weak on this data, which "
        "is exactly why the blocking-miss criterion and the per-severity matrix "
        "are the operative checks (review F-009). FR-6.5's human-corrected "
        "cases are the designed mechanism for growing the non-legitimate side.",
    ]
    return "\n".join(lines) + "\n"
