"""Post-publication probation monitoring and bounded automatic rollback."""

from __future__ import annotations

from typing import Any

from deerflow.config import get_app_config

from .publisher import SkillPublisher
from .signal import TurnAnalysis
from .store import FileEvolutionStore, utc_now_iso


class EvolutionMonitor:
    """Apply task outcomes to the active single-user Skill probation state."""

    def __init__(self, store: FileEvolutionStore | None = None):
        self.store = store or FileEvolutionStore()
        self.publisher = SkillPublisher(self.store)

    def observe(self, analysis: TurnAnalysis) -> list[dict[str, Any]]:
        probations = self.store.get_probations()
        if not probations:
            return []

        outcomes: dict[str, dict[str, Any]] = {}
        for skill in analysis.skills_used:
            name = str(skill.get("name") or "")
            if not name or skill.get("scope") != "custom" or name not in probations:
                continue
            outcome = outcomes.setdefault(name, {"used": False, "failed": False, "reasons": []})
            source = skill.get("source")
            if source == "current_turn":
                outcome["used"] = True
                if analysis.unresolved_error_count:
                    outcome["failed"] = True
                    outcome["reasons"].append("unresolved tool error after Skill use")
            elif source == "previous_turn" and analysis.correction:
                outcome["failed"] = True
                outcome["reasons"].append("explicit user correction after Skill use")

        events: list[dict[str, Any]] = []
        threshold = get_app_config().skill_evolution.monitoring.auto_rollback_consecutive_failures
        for name, outcome in outcomes.items():
            probation = dict(probations[name])
            if probation.get("status") != "probation":
                continue
            if int(probation.get("revision", 0)) != self.store.get_active_revision(name):
                # An out-of-band publication superseded this observation window.
                self.store.set_probation(name, None)
                continue

            if outcome["used"]:
                probation["remaining_uses"] = max(0, int(probation.get("remaining_uses", 0)) - 1)
            if outcome["failed"]:
                probation["consecutive_failures"] = int(probation.get("consecutive_failures", 0)) + 1
                probation["last_failure"] = "; ".join(outcome["reasons"])
            elif outcome["used"]:
                probation["consecutive_failures"] = 0
            probation["last_observed_at"] = utc_now_iso()

            if int(probation.get("consecutive_failures", 0)) >= threshold:
                previous_revision = probation.get("previous_revision")
                if probation.get("auto_published") and previous_revision is not None:
                    result = self.publisher.rollback(
                        name,
                        int(previous_revision),
                        actor="system",
                        note=f"Automatic rollback after {threshold} consecutive probation failures.",
                    )
                    event = {
                        "type": "auto_rollback",
                        "skill_name": name,
                        "failed_revision": probation.get("revision"),
                        "restored_revision": int(previous_revision),
                        "new_revision": result["manifest"]["version"],
                    }
                    self.store.append_audit(actor="system", action="probation.auto_rollback", details=event)
                    events.append(event)
                    continue
                probation["status"] = "alert"
                event = {
                    "type": "regression_alert",
                    "skill_name": name,
                    "revision": probation.get("revision"),
                    "reason": probation.get("last_failure"),
                }
                events.append(event)
                self.store.append_audit(actor="system", action="probation.alert", details=event)
            elif int(probation.get("remaining_uses", 0)) <= 0:
                probation["status"] = "graduated"
                event = {"type": "graduated", "skill_name": name, "revision": probation.get("revision")}
                events.append(event)
                self.store.append_audit(actor="system", action="probation.graduated", details=event)

            self.store.set_probation(name, probation)
        return events
