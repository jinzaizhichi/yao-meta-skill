#!/usr/bin/env python3
import argparse
import html
import json
import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

try:
    from trust_check import permission_governance_status as compute_permission_governance_status
    from trust_check import script_inventory as trust_script_inventory
except ImportError:  # pragma: no cover
    compute_permission_governance_status = None
    trust_script_inventory = None

from review_studio_formatting import registry_package_summary, render_kv_grid
from review_studio_layout import render_review_nav, review_studio_css


ROOT = Path(__file__).resolve().parent.parent


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists() or yaml is None:
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def parse_frontmatter(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end_index = lines[1:].index("---") + 1
    except ValueError:
        return {}
    text = "\n".join(lines[1:end_index])
    if yaml is not None:
        payload = yaml.safe_load(text) or {}
        return payload if isinstance(payload, dict) else {}
    data = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip().strip('"')
    return data


def display_path(skill_dir: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(skill_dir.resolve()))
    except ValueError:
        try:
            return str(path.resolve().relative_to(ROOT.resolve()))
        except ValueError:
            return str(path.resolve())


def link_from(output_html: Path, target: Path) -> str:
    return os.path.relpath(target.resolve(), output_html.parent.resolve())


def report_link(output_html: Path, skill_dir: Path, rel_path: str) -> str:
    return link_from(output_html, skill_dir / rel_path)


def find_line(path: Path, patterns: list[str] | None = None) -> int | None:
    if not path.exists():
        return None
    if not patterns:
        return 1
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 1
    for pattern in patterns:
        for index, line in enumerate(lines, start=1):
            if pattern in line:
                return index
    return 1


def source_refs(
    skill_dir: Path,
    output_html: Path,
    specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for spec in specs:
        rel_path = str(spec.get("path", "")).strip()
        if not rel_path:
            continue
        path = skill_dir / rel_path
        exists = path.exists()
        line = find_line(path, spec.get("patterns", []))
        refs.append(
            {
                "path": rel_path,
                "label": str(spec.get("label", rel_path)),
                "kind": str(spec.get("kind", "source")),
                "line": line,
                "exists": exists,
                "link": link_from(output_html, path) if exists else "",
            }
        )
    return refs


def gate(key: str, label: str, status: str, detail: str, evidence: str, link: str = "") -> dict[str, str]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
        "evidence": evidence,
        "link": link,
    }


def status_label(status: str) -> str:
    return {"pass": "通过", "warn": "关注", "block": "阻断"}.get(status, status)


def add_blockers_from_gate(gates: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    blockers = [item for item in gates if item["status"] == "block"]
    warnings = [item for item in gates if item["status"] == "warn"]
    return blockers, warnings


def target_maturity(skill_dir: Path, overview: dict[str, Any]) -> str:
    manifest = load_json(skill_dir / "manifest.json")
    if manifest.get("maturity_tier"):
        return str(manifest["maturity_tier"])
    metadata = overview.get("metadata", {}) if isinstance(overview, dict) else {}
    if metadata.get("maturity_tier"):
        return str(metadata["maturity_tier"])
    return "scaffold"


def min_output_cases(maturity: str) -> int:
    if maturity in {"library", "governed"}:
        return 5
    if maturity == "production":
        return 3
    return 1


def fallback_permission_governance(skill_dir: Path) -> dict[str, Any]:
    if compute_permission_governance_status is None or trust_script_inventory is None:
        return {}
    try:
        scripts = trust_script_inventory(skill_dir)
        return compute_permission_governance_status(skill_dir, scripts)
    except Exception:
        return {}


def build_gates(skill_dir: Path, output_html: Path, data: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    overview = data["overview"]
    maturity = target_maturity(skill_dir, overview)
    gates: list[dict[str, str]] = []

    intent = data["intent_confidence"]
    intent_score = int(intent.get("score", 0) or 0)
    intent_status = "pass" if intent.get("gate_passed") or intent_score >= 75 else "warn"
    gates.append(
        gate(
            "intent-canvas",
            "意图画布",
            intent_status,
            f"intent confidence {intent_score}/100; {intent.get('recommended_action', 'review current intent frame')}",
            "reports/intent-confidence.json",
            report_link(output_html, skill_dir, "reports/intent-confidence.md"),
        )
    )

    route = data["route_scorecard"]
    route_summary = route.get("summary", {})
    misroutes = int(route_summary.get("misroute_count", len(route.get("misroutes", []))) or 0)
    ambiguous = int(route_summary.get("ambiguous_case_count", len(route.get("ambiguous_cases", []))) or 0)
    if not route:
        route_status = "warn"
        route_detail = "route scorecard is missing; run route-scorecard before release review"
    else:
        route_status = "block" if misroutes else ("warn" if ambiguous else "pass")
        route_detail = f"{route_summary.get('total_cases', 0)} trigger cases; {misroutes} misroutes; {ambiguous} ambiguous"
    gates.append(
        gate(
            "trigger-lab",
            "触发实验",
            route_status,
            route_detail,
            "reports/route_scorecard.json",
            report_link(output_html, skill_dir, "reports/route_scorecard.md"),
        )
    )

    output = data["output_quality"]
    output_execution = data["output_execution"]
    output_blind = data["output_blind_review"]
    output_review = data["output_review_adjudication"]
    output_summary = output.get("summary", {})
    output_execution_summary = output_execution.get("summary", {})
    output_blind_summary = output_blind.get("summary", {})
    output_review_summary = output_review.get("summary", {})
    required_cases = min_output_cases(maturity)
    case_count = int(output_summary.get("case_count", 0) or 0)
    file_backed = int(output_summary.get("file_backed_case_count", 0) or 0)
    near_neighbor = int(output_summary.get("near_neighbor_case_count", 0) or 0)
    boundary = int(output_summary.get("boundary_case_count", 0) or 0)
    blind_pair_count = int(output_blind_summary.get("pair_count", 0) or 0)
    execution_variant_count = int(output_execution_summary.get("variant_run_count", 0) or 0)
    execution_command_count = int(output_execution_summary.get("command_executed_count", 0) or 0)
    execution_model_count = int(output_execution_summary.get("model_executed_count", 0) or 0)
    execution_recorded_count = int(output_execution_summary.get("recorded_fixture_count", 0) or 0)
    review_pair_count = int(output_review_summary.get("pair_count", 0) or 0)
    review_judgment_count = int(output_review_summary.get("judgment_count", 0) or 0)
    review_invalid_count = int(output_review_summary.get("invalid_decision_count", 0) or 0)
    blind_missing = maturity in {"production", "library", "governed"} and (not output_blind or blind_pair_count < case_count)
    execution_failed = bool(output_execution) and (not output_execution.get("ok", True) or int(output_execution_summary.get("failure_count", 0) or 0) > 0)
    review_invalid = bool(output_review) and (not output_review.get("ok", True) or review_invalid_count > 0)
    output_blocked = (
        not output.get("ok", False)
        or not output_summary.get("gate_pass", False)
        or case_count < required_cases
        or execution_failed
        or review_invalid
    )
    output_warn = file_backed == 0 or near_neighbor == 0 or boundary == 0 or blind_missing
    if not output:
        output_status = "warn"
        output_detail = "output eval scorecard is missing; generate it before production review"
    else:
        output_status = "block" if output_blocked else ("warn" if output_warn else "pass")
        output_detail = (
            f"{case_count}/{required_cases} cases; with-skill {output_summary.get('with_skill_pass_rate', 0)}; "
            f"baseline {output_summary.get('baseline_pass_rate', 0)}; file-backed {file_backed}; near-neighbor {near_neighbor}; "
            f"blind A/B {blind_pair_count}"
            + (
                f"; exec {execution_variant_count}; command {execution_command_count}; "
                f"model {execution_model_count}; recorded {execution_recorded_count}"
                if output_execution
                else ""
            )
            + (f"; reviewed {review_judgment_count}/{review_pair_count}" if output_review else "")
        )
    gates.append(
        gate(
            "output-lab",
            "输出实验",
            output_status,
            output_detail,
            "reports/output_quality_scorecard.json",
            report_link(output_html, skill_dir, "reports/output_quality_scorecard.md"),
        )
    )

    context = data["context_budget"]
    context_stats = context.get("stats", {})
    context_status = "pass" if context.get("ok") else "block"
    if context.get("warnings"):
        context_status = "warn" if context_status == "pass" else context_status
    if not context:
        context_status = "warn"
    context_detail = (
        f"initial load {context_stats.get('estimated_initial_load_tokens', 'n/a')}/"
        f"{context_stats.get('context_budget_limit', 'n/a')}; quality density {context_stats.get('quality_density', 'n/a')}"
    )
    gates.append(
        gate(
            "context-budget",
            "上下文",
            context_status,
            context_detail,
            "reports/context_budget.json",
            report_link(output_html, skill_dir, "reports/context_budget.md"),
        )
    )

    conformance = data["conformance"]
    conformance_summary = conformance.get("summary", {})
    fail_count = int(conformance_summary.get("fail_count", 0) or 0)
    if not conformance:
        conformance_status = "warn"
        conformance_detail = "runtime conformance matrix is missing"
    else:
        conformance_status = "block" if fail_count else "pass"
        conformance_detail = f"{conformance_summary.get('pass_count', 0)} / {conformance_summary.get('target_count', 0)} targets pass"
    gates.append(
        gate(
            "runtime-matrix",
            "运行矩阵",
            conformance_status,
            conformance_detail,
            "reports/conformance_matrix.json",
            report_link(output_html, skill_dir, "reports/conformance_matrix.md"),
        )
    )

    trust = data["trust"]
    trust_summary = trust.get("summary", {})
    if not trust:
        trust_status = "warn"
        trust_detail = "security trust report is missing"
    else:
        trust_status = "block" if trust.get("failures") else ("warn" if trust.get("warnings") else "pass")
        trust_detail = (
            f"{trust_summary.get('secret_findings', 0)} secrets; "
            f"{trust_summary.get('script_count', 0)} scripts; "
            f"{trust_summary.get('network_script_count', 0)} network-capable scripts; "
            f"{trust_summary.get('help_smoke_failed_count', 0)} help smoke failures"
        )
    gates.append(
        gate(
            "trust-report",
            "信任报告",
            trust_status,
            trust_detail,
            "reports/security_trust_report.json",
            report_link(output_html, skill_dir, "reports/security_trust_report.md"),
        )
    )

    permission_governance = trust.get("permission_governance", {}) if isinstance(trust.get("permission_governance", {}), dict) else {}
    if trust and not permission_governance:
        permission_governance = fallback_permission_governance(skill_dir)
    if not trust:
        permission_status = "warn"
        permission_detail = "permission governance evidence is missing because trust report is missing"
    elif not permission_governance:
        permission_status = "warn"
        permission_detail = "permission governance evidence is missing from trust report"
    else:
        required = int(permission_governance.get("required_count", 0) or 0)
        approved = int(permission_governance.get("approval_count", 0) or 0)
        gaps = (
            int(permission_governance.get("missing_count", 0) or 0)
            + int(permission_governance.get("invalid_count", 0) or 0)
            + int(permission_governance.get("expired_count", 0) or 0)
        )
        if gaps:
            permission_status = "block" if maturity == "governed" else "warn"
        else:
            permission_status = "pass"
        required_names = ", ".join(permission_governance.get("required_capabilities", []) or []) or "none"
        permission_detail = f"{approved}/{required} permissions approved; gaps {gaps}; required {required_names}"
    gates.append(
        gate(
            "permission-gates",
            "权限批准",
            permission_status,
            permission_detail,
            "reports/security_trust_report.json + security/permission_policy.json",
            report_link(output_html, skill_dir, "security/permission_policy.md"),
        )
    )

    runtime_permissions = data["runtime_permissions"]
    runtime_permissions_summary = runtime_permissions.get("summary", {})
    if not runtime_permissions:
        runtime_permission_status = "block" if maturity == "governed" else "warn"
        runtime_permission_detail = "runtime permission probe report is missing"
    elif runtime_permissions.get("failures"):
        runtime_permission_status = "block"
        runtime_permission_detail = f"{runtime_permissions_summary.get('failure_count', len(runtime_permissions.get('failures', [])))} runtime permission probe failures"
    else:
        runtime_permission_status = "pass"
        runtime_permission_detail = (
            f"{runtime_permissions_summary.get('pass_count', 0)}/{runtime_permissions_summary.get('target_count', 0)} targets probed; "
            f"native {runtime_permissions_summary.get('native_enforcement_count', 0)}; "
            f"metadata fallback {runtime_permissions_summary.get('metadata_fallback_count', 0)}; "
            f"residual risks {runtime_permissions_summary.get('residual_risk_count', 0)}"
        )
    gates.append(
        gate(
            "permission-runtime",
            "权限探针",
            runtime_permission_status,
            runtime_permission_detail,
            "reports/runtime_permission_probes.json",
            report_link(output_html, skill_dir, "reports/runtime_permission_probes.md"),
        )
    )

    atlas = data["atlas"]
    atlas_summary = atlas.get("summary", {})
    actionable_route_collisions = int(
        atlas_summary.get("actionable_route_collision_count", atlas_summary.get("route_collision_count", 0)) or 0
    )
    actionable_owner_gaps = int(atlas_summary.get("actionable_owner_gap_count", atlas_summary.get("owner_gap_count", 0)) or 0)
    actionable_stale = int(atlas_summary.get("actionable_stale_count", atlas_summary.get("stale_count", 0)) or 0)
    atlas_issues = actionable_route_collisions + actionable_owner_gaps + actionable_stale
    if not atlas:
        atlas_status = "warn"
        atlas_detail = "skill atlas is missing; portfolio-level conflicts are unknown"
    else:
        atlas_status = "warn" if atlas_issues else "pass"
        atlas_detail = (
            f"{atlas_summary.get('skill_count', 0)} skills, "
            f"{atlas_summary.get('actionable_skill_count', atlas_summary.get('skill_count', 0))} actionable; "
            f"{actionable_route_collisions} actionable route collisions; "
            f"{actionable_owner_gaps} actionable owner gaps; "
            f"{actionable_stale} actionable stale; "
            f"{atlas_summary.get('non_actionable_issue_count', 0)} scoped non-actionable issues"
        )
    gates.append(
        gate(
            "skill-atlas",
            "组合治理",
            atlas_status,
            atlas_detail,
            "reports/skill_atlas.json",
            report_link(output_html, skill_dir, "reports/skill_atlas.html"),
        )
    )

    adoption = data["adoption_drift"]
    adoption_summary = adoption.get("summary", {})
    if not adoption:
        adoption_status = "warn"
        adoption_detail = "adoption drift report is missing; real usage impact is unknown"
    elif adoption.get("failures"):
        adoption_status = "block"
        adoption_detail = f"telemetry privacy or schema failures: {len(adoption.get('failures', []))}"
    else:
        risk_band = adoption_summary.get("risk_band", "no-data")
        adoption_status = "warn" if risk_band in {"no-data", "medium", "high"} else "pass"
        adoption_detail = (
            f"{adoption_summary.get('event_count', 0)} metadata events; "
            f"adoption {adoption_summary.get('adoption_rate', 0)}; "
            f"missed {adoption_summary.get('missed_trigger_count', 0)}; "
            f"bad-output {adoption_summary.get('bad_output_count', 0)}; "
            f"risk {risk_band}"
        )
    gates.append(
        gate(
            "operations-loop",
            "运营回路",
            adoption_status,
            adoption_detail,
            "reports/adoption_drift_report.json",
            report_link(output_html, skill_dir, "reports/adoption_drift_report.md"),
        )
    )

    waiver = data["review_waivers"]
    waiver_summary = waiver.get("summary", {})
    active_covered = set(waiver_summary.get("covered_gate_keys", []) or [])
    prior_blockers = [item for item in gates if item["status"] == "block"]
    prior_warnings = [item for item in gates if item["status"] == "warn"]
    unwaived_warnings = [item for item in prior_warnings if item["key"] not in active_covered]
    if not waiver:
        waiver_status = "warn"
        waiver_detail = "review waiver ledger is missing; warning acceptance is not auditable"
    elif waiver.get("failures"):
        waiver_status = "block"
        waiver_detail = f"{len(waiver.get('failures', []))} invalid waiver records"
    elif prior_blockers:
        waiver_status = "block"
        waiver_detail = f"{len(prior_blockers)} blocker gates cannot be waived in v0"
    elif unwaived_warnings:
        waiver_status = "warn"
        waiver_detail = (
            f"{waiver_summary.get('active_count', 0)} active waivers; "
            f"{len(unwaived_warnings)} warning gates still need reviewer decision"
        )
    else:
        waiver_status = "pass"
        waiver_detail = f"{waiver_summary.get('active_count', 0)} active waivers cover current warnings"
    gates.append(
        gate(
            "review-waivers",
            "人工批准",
            waiver_status,
            waiver_detail,
            "reports/review_waivers.json",
            report_link(output_html, skill_dir, "reports/review_waivers.md"),
        )
    )

    registry = data["registry"]
    install = data["install_simulation"]
    if not registry:
        if maturity in {"library", "governed"}:
            registry_status = "warn"
            registry_detail = "registry audit is missing; package metadata is not reviewable"
        else:
            registry_status = "pass"
            registry_detail = "registry audit is optional until team distribution is required"
    else:
        compatibility = registry.get("package", {}).get("compatibility", {})
        pass_count = sum(1 for status in compatibility.values() if status == "pass")
        registry_status = "block" if registry.get("failures") else ("warn" if registry.get("warnings") else "pass")
        registry_detail = (
            f"{registry.get('package', {}).get('name', 'package')} "
            f"{registry.get('package', {}).get('version', 'n/a')}; "
            f"{pass_count}/{len(compatibility)} compatibility entries pass"
        )
    if install:
        if install.get("failures"):
            registry_status = "block"
        install_summary = install.get("summary", {})
        registry_detail += (
            f"; install {'pass' if install.get('ok') else 'fail'}"
            f" with {install_summary.get('adapter_count', 0)} adapters"
        )
    gates.append(
        gate(
            "registry-audit",
            "注册审计",
            registry_status,
            registry_detail,
            "reports/registry_audit.json + reports/install_simulation.json",
            report_link(output_html, skill_dir, "reports/registry_audit.md"),
        )
    )

    promotion = data["promotion"]
    migration_path = ROOT / "docs" / "migration-v2.md"
    if promotion:
        promotion_summary = promotion.get("summary", {})
        blocked = int(promotion_summary.get("blocked", 0) or 0)
        release_status = "block" if blocked else "pass"
        release_detail = f"{promotion_summary.get('promote', 0)} promote; {promotion_summary.get('keep_current', 0)} keep current; {blocked} blocked"
    else:
        release_status = "warn"
        release_detail = "promotion decisions are missing; release notes need reviewer confirmation"
    upgrade = data["upgrade_check"]
    if upgrade:
        upgrade_summary = upgrade.get("summary", {})
        if upgrade.get("failures"):
            release_status = "block"
        elif upgrade.get("warnings") and release_status == "pass":
            release_status = "warn"
        release_detail += (
            f"; upgrade {upgrade_summary.get('declared_bump', 'n/a')}"
            f" declared / {upgrade_summary.get('recommended_bump', 'n/a')} recommended"
        )
    gates.append(
        gate(
            "release-notes",
            "发布路线",
            release_status,
            release_detail,
            "reports/promotion_decisions.json + reports/upgrade_check.json + docs/migration-v2.md",
            report_link(output_html, skill_dir, "reports/promotion_decisions.md") if promotion else str(migration_path),
        )
    )

    return gates


def weighted_score(gates: list[dict[str, str]]) -> int:
    weights = {
        "trigger-lab": 15,
        "output-lab": 20,
        "context-budget": 10,
        "runtime-matrix": 10,
        "trust-report": 10,
        "permission-gates": 10,
        "permission-runtime": 10,
        "skill-atlas": 10,
        "operations-loop": 10,
        "review-waivers": 10,
        "registry-audit": 10,
        "release-notes": 10,
        "intent-canvas": 10,
    }
    earned = 0.0
    total = 0.0
    for item in gates:
        weight = weights.get(item["key"], 5)
        total += weight
        if item["status"] == "pass":
            earned += weight
        elif item["status"] == "warn":
            earned += weight * 0.6
    return int(round(earned / total * 100)) if total else 0


def evidence_paths(skill_dir: Path) -> dict[str, str]:
    rels = {
        "skill_overview": "reports/skill-overview.html",
        "review_viewer": "reports/review-viewer.html",
        "output_eval": "reports/output_quality_scorecard.md",
        "output_execution": "reports/output_execution_runs.md",
        "output_blind_review": "reports/output_blind_review_pack.md",
        "output_review_adjudication": "reports/output_review_adjudication.md",
        "runtime_conformance": "reports/conformance_matrix.md",
        "trust_report": "reports/security_trust_report.md",
        "permission_policy": "security/permission_policy.md",
        "runtime_permissions": "reports/runtime_permission_probes.md",
        "skill_atlas": "reports/skill_atlas.html",
        "compiled_targets": "reports/compiled_targets.md",
        "adoption_drift": "reports/adoption_drift_report.md",
        "review_waivers": "reports/review_waivers.md",
        "review_annotations": "reports/review_annotations.md",
        "registry_audit": "reports/registry_audit.md",
        "package_verification": "reports/package_verification.md",
        "install_simulation": "reports/install_simulation.md",
        "upgrade_check": "reports/upgrade_check.md",
        "migration": "docs/migration-v2.md",
        "skill_ir": "reports/skill-ir.json",
    }
    return {key: rel for key, rel in rels.items() if (skill_dir / rel).exists() or (ROOT / rel).exists()}


def load_review_data(skill_dir: Path) -> dict[str, dict[str, Any]]:
    reports = skill_dir / "reports"
    return {
        "overview": load_json(reports / "skill-overview.json"),
        "intent_confidence": load_json(reports / "intent-confidence.json"),
        "intent_dialogue": load_json(reports / "intent-dialogue.json"),
        "route_scorecard": load_json(reports / "route_scorecard.json"),
        "output_quality": load_json(reports / "output_quality_scorecard.json"),
        "output_execution": load_json(reports / "output_execution_runs.json"),
        "output_blind_review": load_json(reports / "output_blind_review_pack.json"),
        "output_review_adjudication": load_json(reports / "output_review_adjudication.json"),
        "compiled_targets": load_json(reports / "compiled_targets.json"),
        "conformance": load_json(reports / "conformance_matrix.json"),
        "runtime_permissions": load_json(reports / "runtime_permission_probes.json"),
        "trust": load_json(reports / "security_trust_report.json"),
        "context_budget": load_json(reports / "context_budget.json"),
        "promotion": load_json(reports / "promotion_decisions.json"),
        "atlas": load_json(reports / "skill_atlas.json"),
        "adoption_drift": load_json(reports / "adoption_drift_report.json"),
        "review_waivers": load_json(reports / "review_waivers.json"),
        "review_annotations": load_json(reports / "review_annotations.json"),
        "registry": load_json(reports / "registry_audit.json"),
        "package_verification": load_json(reports / "package_verification.json"),
        "install_simulation": load_json(reports / "install_simulation.json"),
        "upgrade_check": load_json(reports / "upgrade_check.json"),
        "manifest": load_json(skill_dir / "manifest.json"),
        "frontmatter": parse_frontmatter(skill_dir / "SKILL.md"),
        "interface": load_yaml(skill_dir / "agents" / "interface.yaml"),
    }


def insight_cards(data: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    overview = data["overview"]
    output = data["output_quality"].get("summary", {})
    output_execution = data["output_execution"].get("summary", {})
    output_blind = data["output_blind_review"].get("summary", {})
    output_review = data["output_review_adjudication"].get("summary", {})
    compiled = data["compiled_targets"].get("summary", {})
    conformance = data["conformance"].get("summary", {})
    runtime_permissions = data["runtime_permissions"].get("summary", {})
    trust = data["trust"].get("summary", {})
    atlas = data["atlas"].get("summary", {})
    adoption = data["adoption_drift"].get("summary", {})
    waivers = data["review_waivers"].get("summary", {})
    annotations = data["review_annotations"].get("summary", {})
    registry = data["registry"].get("package", {})
    package_verification = data["package_verification"].get("summary", {})
    install_simulation = data["install_simulation"].get("summary", {})
    upgrade = data["upgrade_check"].get("summary", {})
    cards = [
        {
            "label": "Skill IR",
            "value": str(overview.get("skill_ir", {}).get("schema_version", "missing")),
            "detail": f"{overview.get('skill_ir', {}).get('target_count', 0)} targets in platform-neutral contract",
        },
        {
            "label": "Compiler",
            "value": f"{compiled.get('pass_count', 0)}/{compiled.get('target_count', 0)}",
            "detail": "target contracts compiled from Skill IR",
        },
        {
            "label": "Output Delta",
            "value": str(output.get("delta", "n/a")),
            "detail": f"{output.get('case_count', 0)} cases; {output.get('file_backed_case_count', 0)} file-backed",
        },
        {
            "label": "Exec Runs",
            "value": str(output_execution.get("variant_run_count", 0)),
            "detail": (
                f"command {output_execution.get('command_executed_count', 0)}; "
                f"model {output_execution.get('model_executed_count', 0)}; "
                f"recorded {output_execution.get('recorded_fixture_count', 0)}"
            ),
        },
        {
            "label": "Blind A/B",
            "value": str(output_blind.get("pair_count", 0)),
            "detail": "review pairs hide baseline vs with-skill labels",
        },
        {
            "label": "Review A/B",
            "value": f"{output_review.get('judgment_count', 0)}/{output_review.get('pair_count', 0)}",
            "detail": f"adjudication decisions; pending {output_review.get('pending_count', 0)}",
        },
        {
            "label": "Runtime",
            "value": f"{conformance.get('pass_count', 0)}/{conformance.get('target_count', 0)}",
            "detail": "target conformance pass rate",
        },
        {
            "label": "Perm Probe",
            "value": f"{runtime_permissions.get('metadata_fallback_count', 0)}/{runtime_permissions.get('target_count', 0)}",
            "detail": f"{runtime_permissions.get('native_enforcement_count', 0)} native enforcement targets",
        },
        {
            "label": "Trust",
            "value": str(trust.get("secret_findings", 0)),
            "detail": f"{trust.get('script_count', 0)} scripts scanned; secrets found",
        },
        {
            "label": "Atlas",
            "value": str(atlas.get("route_collision_count", 0)),
            "detail": f"{atlas.get('skill_count', 0)} scanned skills; route collisions",
        },
        {
            "label": "Drift",
            "value": str(adoption.get("risk_band", "n/a")),
            "detail": f"{adoption.get('event_count', 0)} metadata events; {adoption.get('missed_trigger_count', 0)} missed triggers",
        },
        {
            "label": "Waivers",
            "value": str(waivers.get("active_count", 0)),
            "detail": f"{waivers.get('covered_gate_count', 0)} gates covered; human risk decisions",
        },
        {
            "label": "Notes",
            "value": f"{annotations.get('open_count', 0)}/{annotations.get('annotation_count', 0)}",
            "detail": f"{annotations.get('open_blocker_count', 0)} open blocker annotations",
        },
        {
            "label": "Registry",
            "value": str(registry.get("version", "n/a")),
            "detail": f"{len(registry.get('targets', []))} targets; {registry.get('license', 'no license')} license",
        },
        {
            "label": "Archive",
            "value": "pass" if data["package_verification"].get("ok") else "n/a",
            "detail": f"{package_verification.get('archive_entry_count', 0)} zip entries; package verification",
        },
        {
            "label": "Install",
            "value": "pass" if data["install_simulation"].get("ok") else "n/a",
            "detail": f"{install_simulation.get('adapter_count', 0)} adapters readable; local install simulation",
        },
        {
            "label": "Upgrade",
            "value": str(upgrade.get("recommended_bump", "n/a")),
            "detail": f"declared {upgrade.get('declared_bump', 'n/a')}; {upgrade.get('breaking_change_count', 0)} breaking changes",
        },
    ]
    return cards


def render_gate_list(gates: list[dict[str, str]]) -> str:
    items = []
    for item in gates:
        link_html = f"<a href='{html.escape(item['link'])}'>证据</a>" if item.get("link") else ""
        items.append(
            "<article class='gate "
            + html.escape(item["status"])
            + "'>"
            f"<div><span>{html.escape(status_label(item['status']))}</span><h3>{html.escape(item['label'])}</h3></div>"
            f"<p>{html.escape(item['detail'])}</p>"
            f"<footer>{html.escape(item['evidence'])} {link_html}</footer>"
            "</article>"
        )
    return "".join(items)


def render_insights(cards: list[dict[str, str]]) -> str:
    return "".join(
        (
            "<article class='metric'>"
            f"<span>{html.escape(item['label'])}</span>"
            f"<strong>{html.escape(item['value'])}</strong>"
            f"<p>{html.escape(item['detail'])}</p>"
            "</article>"
        )
        for item in cards
    )


def render_issue_list(title: str, items: list[dict[str, str]]) -> str:
    if not items:
        return f"<section><h2>{html.escape(title)}</h2><p class='muted'>无。</p></section>"
    body = "".join(
        (
            "<li>"
            f"<strong>{html.escape(item['label'])}</strong>"
            f"<span>{html.escape(item['detail'])}</span>"
            "</li>"
        )
        for item in items
    )
    return f"<section><h2>{html.escape(title)}</h2><ul class='issues'>{body}</ul></section>"


ACTION_GUIDANCE: dict[str, dict[str, str]] = {
    "intent-canvas": {
        "summary": "收紧真实任务、输入、输出、排除项和成功标准。",
        "why": "低 intent confidence 会让后续 Skill IR、输出评测和 Review Studio 结论建立在模糊意图上。",
        "source_fix": "reports/intent-dialogue.md + reports/intent-confidence.md",
        "source_paths": [
            {"path": "reports/intent-dialogue.md", "label": "intent dialogue", "kind": "report", "patterns": ["# Intent"]},
            {"path": "reports/intent-confidence.md", "label": "intent confidence", "kind": "report", "patterns": ["# Intent"]},
        ],
        "verification": "python3 scripts/yao.py intent-confidence .",
    },
    "trigger-lab": {
        "summary": "修正 route scorecard 中的误触发、漏触发或 ambiguous case。",
        "why": "触发错误会让正确 Skill 失活，或让相邻 Skill 被错误调用。",
        "source_fix": "SKILL.md frontmatter description + evals/*/trigger_cases.json",
        "source_paths": [
            {"path": "SKILL.md", "label": "frontmatter description", "kind": "source", "patterns": ["description:"]},
            {"path": "evals/trigger_cases.json", "label": "trigger eval cases", "kind": "eval", "patterns": ["should_trigger"]},
            {"path": "reports/route_scorecard.md", "label": "route scorecard", "kind": "report", "patterns": ["# Route"]},
        ],
        "verification": "python3 scripts/build_confusion_matrix.py",
    },
    "output-lab": {
        "summary": "补足 output eval 的 case 数、file-backed case、near-neighbor case 和 boundary case。",
        "why": "没有输出质量证据时，Skill 只能证明会触发，不能证明输出真的更好。",
        "source_fix": "evals/output/cases.jsonl + reports/output_quality_scorecard.md",
        "source_paths": [
            {"path": "evals/output/cases.jsonl", "label": "output eval cases", "kind": "eval", "patterns": ["case_id"]},
            {"path": "reports/output_quality_scorecard.md", "label": "output scorecard", "kind": "report", "patterns": ["# Output"]},
            {"path": "reports/output_execution_runs.md", "label": "output execution runs", "kind": "report", "patterns": ["# Output Execution"]},
            {"path": "reports/output_blind_review_pack.md", "label": "blind A/B review pack", "kind": "report", "patterns": ["# Output Blind"]},
            {"path": "reports/output_review_adjudication.md", "label": "review adjudication", "kind": "report", "patterns": ["# Output Review"]},
        ],
        "verification": "python3 scripts/run_output_execution.py",
    },
    "context-budget": {
        "summary": "压缩入口与高成本 references，保留最小可路由上下文。",
        "why": "上下文成本过高会降低加载稳定性，并挤压用户任务材料的预算。",
        "source_fix": "SKILL.md + references/",
        "source_paths": [
            {"path": "SKILL.md", "label": "entrypoint", "kind": "source", "patterns": ["# Yao Meta Skill"]},
            {"path": "reports/context_budget.md", "label": "context budget", "kind": "report", "patterns": ["# Context"]},
        ],
        "verification": "python3 scripts/render_context_reports.py",
    },
    "runtime-matrix": {
        "summary": "修复目标端结构、metadata、相对路径、fallback 或 adapter target 声明。",
        "why": "runtime conformance 失败意味着包可能被目标客户端错误加载或静默降级。",
        "source_fix": "agents/interface.yaml + reports/conformance_matrix.md",
        "source_paths": [
            {"path": "agents/interface.yaml", "label": "portable interface", "kind": "source", "patterns": ["adapter_targets"]},
            {"path": "reports/conformance_matrix.md", "label": "conformance matrix", "kind": "report", "patterns": ["# Runtime"]},
        ],
        "verification": "python3 scripts/run_conformance_suite.py .",
    },
    "trust-report": {
        "summary": "处理脚本 help surface、依赖 pin、network policy、secret 和权限声明。",
        "why": "团队分发时，脚本和依赖是主要供应链风险面，warning 必须有明确处置。",
        "source_fix": "reports/security_trust_report.md + security/*.md + scripts/",
        "source_paths": [
            {"path": "reports/security_trust_report.md", "label": "trust report", "kind": "report", "patterns": ["# Security"]},
            {"path": "security/script_policy.md", "label": "script policy", "kind": "policy", "patterns": ["# Script"]},
            {"path": "security/network_policy.md", "label": "network policy", "kind": "policy", "patterns": ["# Network"]},
        ],
        "verification": "python3 scripts/trust_check.py .",
    },
    "permission-gates": {
        "summary": "补齐高权限能力的 reviewer、scope、reason、expires_at 和目标端 enforcement 说明。",
        "why": "权限契约只有在批准人、有效期和目标端处置方式明确时，才能支撑 governed release。",
        "source_fix": "security/permission_policy.json + security/permission_policy.md",
        "source_paths": [
            {"path": "security/permission_policy.json", "label": "permission approvals", "kind": "policy", "patterns": ["approved"]},
            {"path": "security/permission_policy.md", "label": "permission method", "kind": "policy", "patterns": ["# Permission"]},
        ],
        "verification": "python3 scripts/trust_check.py .",
    },
    "permission-runtime": {
        "summary": "生成并修复目标包的 runtime permission probe 报告。",
        "why": "目标端即使只能提供 metadata fallback，也必须明确 native enforcement 缺口、表示位置和 operator note。",
        "source_fix": "dist/targets/*/adapter.json + reports/runtime_permission_probes.md",
        "source_paths": [
            {"path": "reports/runtime_permission_probes.md", "label": "runtime permission probes", "kind": "report", "patterns": ["# Runtime"]},
            {"path": "dist/targets/openai/adapter.json", "label": "OpenAI adapter", "kind": "package", "patterns": ["target_permission_contract"]},
            {"path": "dist/targets/claude/adapter.json", "label": "Claude adapter", "kind": "package", "patterns": ["target_permission_contract"]},
            {"path": "dist/targets/generic/adapter.json", "label": "generic adapter", "kind": "package", "patterns": ["target_permission_contract"]},
        ],
        "verification": "python3 scripts/probe_runtime_permissions.py . --package-dir dist",
    },
    "skill-atlas": {
        "summary": "处理 portfolio 里的路由冲突、owner 缺口、stale skill 和重复能力。",
        "why": "单个 Skill 质量很高仍可能在团队 skill library 中被相邻 Skill 冲突削弱。",
        "source_fix": "reports/skill_atlas.html + skill_atlas/catalog.json",
        "source_paths": [
            {"path": "skill_atlas/catalog.json", "label": "skill atlas catalog", "kind": "atlas", "patterns": ["summary"]},
            {"path": "skill_atlas/policy.json", "label": "atlas scope policy", "kind": "policy", "patterns": ["scope"]},
            {"path": "reports/skill_atlas.html", "label": "skill atlas report", "kind": "report", "patterns": ["Skill Atlas"]},
        ],
        "verification": "python3 scripts/build_skill_atlas.py --workspace-root .",
    },
    "operations-loop": {
        "summary": "记录 metadata-only 使用事件，或明确当前 release 缺少真实使用信号。",
        "why": "没有运营回路时，reviewer 无法判断采用率、误触发、坏输出和 review overdue 的真实影响。",
        "source_fix": "reports/adoption_drift_report.md",
        "source_paths": [
            {"path": "reports/adoption_drift_report.md", "label": "adoption drift report", "kind": "report", "patterns": ["# Adoption"]},
            {"path": "references/telemetry-drift-method.md", "label": "telemetry method", "kind": "method", "patterns": ["# Telemetry"]},
        ],
        "verification": "python3 scripts/render_adoption_drift_report.py . --record-event skill_activation --activation-type explicit --outcome accepted",
    },
    "review-waivers": {
        "summary": "对保留的 warning 写入 reviewer、理由、范围和到期时间，或修掉 warning。",
        "why": "warning 可以被接受，但必须可审计、会过期，并且不能掩盖 blocker。",
        "source_fix": "reports/review_waivers.md",
        "source_paths": [
            {"path": "reports/review_waivers.md", "label": "waiver ledger", "kind": "report", "patterns": ["# Review"]},
            {"path": "references/review-waiver-method.md", "label": "waiver method", "kind": "method", "patterns": ["# Review"]},
        ],
        "verification": "python3 scripts/render_review_waivers.py .",
    },
    "registry-audit": {
        "summary": "补齐 registry package metadata、checksum、license、owner、review cadence 和 install evidence。",
        "why": "分发元数据不完整时，团队无法安全安装、升级或追溯包体来源。",
        "source_fix": "registry/ + reports/registry_audit.md",
        "source_paths": [
            {"path": "registry/packages/yao-meta-skill.json", "label": "registry package", "kind": "registry", "patterns": ["version"]},
            {"path": "reports/registry_audit.md", "label": "registry audit", "kind": "report", "patterns": ["# Registry"]},
            {"path": "reports/install_simulation.md", "label": "install simulation", "kind": "report", "patterns": ["# Install"]},
        ],
        "verification": "python3 scripts/registry_audit.py .",
    },
    "release-notes": {
        "summary": "确认 promotion、upgrade diff、breaking changes、migration guide 和 known limitations。",
        "why": "发布说明不完整会让使用者无法判断升级风险和迁移动作。",
        "source_fix": "reports/upgrade_check.md + docs/migration-v2.md",
        "source_paths": [
            {"path": "reports/upgrade_check.md", "label": "upgrade check", "kind": "report", "patterns": ["# Upgrade"]},
            {"path": "docs/migration-v2.md", "label": "migration guide", "kind": "docs", "patterns": ["# Migration"]},
            {"path": "reports/promotion_decisions.md", "label": "promotion decisions", "kind": "report", "patterns": ["# Promotion"]},
        ],
        "verification": "python3 scripts/upgrade_check.py . --previous-package-json registry/examples/yao-meta-skill-1.0.0.json",
    },
}


def build_review_actions(gates: list[dict[str, str]], skill_dir: Path, output_html: Path) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for gate_item in gates:
        if gate_item["status"] == "pass":
            continue
        guidance = ACTION_GUIDANCE.get(
            gate_item["key"],
            {
                "summary": "打开证据报告，修复当前 gate 暴露的问题。",
                "why": "该 gate 未通过，release reviewer 需要明确处置动作。",
                "source_fix": gate_item.get("evidence", ""),
                "source_paths": [],
                "verification": "python3 scripts/render_review_studio.py .",
            },
        )
        refs = source_refs(skill_dir, output_html, guidance.get("source_paths", []))
        actions.append(
            {
                "gate_key": gate_item["key"],
                "label": gate_item["label"],
                "status": gate_item["status"],
                "priority": "blocker" if gate_item["status"] == "block" else "warning",
                "summary": guidance["summary"],
                "why": guidance["why"],
                "source_fix": guidance["source_fix"],
                "source_refs": refs,
                "evidence": gate_item.get("evidence", ""),
                "evidence_link": gate_item.get("link", ""),
                "verification_command": guidance["verification"],
            }
        )
    return actions


def render_action_source_refs(refs: list[dict[str, Any]]) -> str:
    if not refs:
        return "<p class='muted'>暂无结构化 source refs；请先打开证据报告。</p>"
    items = []
    for ref in refs:
        line_suffix = f":{ref['line']}" if ref.get("line") else ""
        label = f"{ref['path']}{line_suffix}"
        if ref.get("exists") and ref.get("link"):
            path_html = f"<a href='{html.escape(ref['link'])}'>{html.escape(label)}</a>"
        else:
            path_html = f"<span>{html.escape(label)} · missing</span>"
        items.append(
            "<li>"
            f"{path_html}"
            f"<small>{html.escape(ref.get('label', 'source'))} · {html.escape(ref.get('kind', 'source'))}</small>"
            "</li>"
        )
    return "<ul class='source-ref-list'>" + "".join(items) + "</ul>"


def render_review_actions(actions: list[dict[str, Any]]) -> str:
    if not actions:
        return "<p class='muted'>当前没有 blocker 或 warning。保持现有证据链即可。</p>"
    cards = []
    for item in actions:
        link_html = f"<a href='{html.escape(item['evidence_link'])}'>打开证据</a>" if item.get("evidence_link") else ""
        source_refs_html = render_action_source_refs(item.get("source_refs", []))
        cards.append(
            "<article class='action-card "
            + html.escape(item["status"])
            + "'>"
            f"<div><span>{html.escape(status_label(item['status']))}</span><h3>{html.escape(item['label'])}</h3></div>"
            f"<p>{html.escape(item['summary'])}</p>"
            f"<small>{html.escape(item['why'])}</small>"
            f"<dl><dt>修复位置</dt><dd>{html.escape(item['source_fix'])}</dd>"
            f"<dt>验证命令</dt><dd><code>{html.escape(item['verification_command'])}</code></dd></dl>"
            f"{source_refs_html}"
            f"<footer>{html.escape(item['evidence'])} {link_html}</footer>"
            "</article>"
        )
    return "".join(cards)


def render_review_annotations_panel(annotations_report: dict[str, Any]) -> str:
    annotations = annotations_report.get("annotations", []) if isinstance(annotations_report, dict) else []
    if not annotations:
        return "<p class='muted'>当前没有 reviewer 批注。</p>"
    cards = []
    for item in annotations:
        line_suffix = f":{item['line']}" if item.get("line") else ""
        target_label = f"{item.get('target_path', '')}{line_suffix}"
        meta = " · ".join(
            part
            for part in [
                str(item.get("gate_key", "")),
                str(item.get("reviewer", "")),
                str(item.get("created_at", "")),
            ]
            if part
        )
        cards.append(
            "<article class='annotation-card "
            + html.escape(str(item.get("severity", "note")))
            + " "
            + html.escape(str(item.get("status", "open")))
            + "'>"
            f"<div><span>{html.escape(str(item.get('severity', 'note')))} · {html.escape(str(item.get('status', 'open')))}</span>"
            f"<h3>{html.escape(str(item.get('id', 'annotation')))}</h3></div>"
            f"<p>{html.escape(str(item.get('body', '')))}</p>"
            f"<dl><dt>位置</dt><dd><code>{html.escape(target_label)}</code></dd>"
            f"<dt>Gate</dt><dd>{html.escape(str(item.get('gate_key', '')))}</dd>"
            f"<dt>建议</dt><dd>{html.escape(str(item.get('suggested_action', '') or '无'))}</dd></dl>"
            f"<small>{html.escape(meta)}</small>"
            f"<footer>{html.escape(str(item.get('source_excerpt', '')))}</footer>"
            "</article>"
        )
    return "".join(cards)


def render_html(report: dict[str, Any]) -> str:
    summary = report["summary"]
    gates = report["gates"]
    gate_details = {item["key"]: item["detail"] for item in gates}
    blockers = report["blockers"]
    warnings = report["warnings"]
    insights = insight_cards(report["data"])
    overview = report["data"]["overview"]
    manifest = report["data"]["manifest"]
    frontmatter = report["data"]["frontmatter"]
    title = overview.get("display_name") or overview.get("title") or frontmatter.get("name") or manifest.get("name") or "Skill"
    description = overview.get("description") or frontmatter.get("description", "")
    nav_html = render_review_nav()
    gates_html = render_gate_list(gates)
    metrics_html = render_insights(insights)
    blockers_html = render_issue_list("阻断事项", blockers)
    warnings_html = render_issue_list("关注事项", warnings)
    actions_html = render_review_actions(report["review_actions"])
    annotations_html = render_review_annotations_panel(report["data"].get("review_annotations", {}))
    output_summary = report["data"]["output_quality"].get("summary", {})
    output_execution_summary = report["data"]["output_execution"].get("summary", {})
    output_blind_summary = report["data"]["output_blind_review"].get("summary", {})
    output_review_summary = report["data"]["output_review_adjudication"].get("summary", {})
    conformance_summary = report["data"]["conformance"].get("summary", {})
    compiled_summary = report["data"]["compiled_targets"].get("summary", {})
    trust_summary = report["data"]["trust"].get("summary", {})
    runtime_permissions_summary = report["data"]["runtime_permissions"].get("summary", {})
    atlas_summary = report["data"]["atlas"].get("summary", {})
    adoption_summary = report["data"]["adoption_drift"].get("summary", {})
    waiver_summary = report["data"]["review_waivers"].get("summary", {})
    annotation_summary = report["data"]["review_annotations"].get("summary", {})
    annotation_caption = (
        f"{annotation_summary.get('annotation_count', 0)} 条批注；"
        f"开放 {annotation_summary.get('open_count', 0)}；"
        f"阻断 {annotation_summary.get('open_blocker_count', 0)}"
    )
    registry_package = report["data"]["registry"].get("package", {})
    package_summary = report["data"]["package_verification"].get("summary", {})
    atlas_panel = render_kv_grid(
        atlas_summary,
        [
            "skill_count",
            "actionable_skill_count",
            "actionable_route_collision_count",
            "actionable_owner_gap_count",
            "actionable_stale_count",
            "non_actionable_issue_count",
        ],
        "skill atlas summary missing",
    )
    output_panel = render_kv_grid(
        output_summary,
        ["case_count", "with_skill_pass_rate", "baseline_pass_rate", "delta", "gate_pass", "failure_count"],
        "output eval scorecard missing",
    )
    execution_panel = render_kv_grid(
        output_execution_summary,
        [
            "variant_run_count",
            "command_executed_count",
            "model_executed_count",
            "recorded_fixture_count",
            "timing_observed_count",
            "token_estimated_count",
        ],
        "output execution report missing",
    )
    blind_panel = render_kv_grid(
        output_blind_summary,
        ["pair_count", "answer_key_separate", "with_skill_hidden_count"],
        "blind A/B review pack missing",
    )
    review_panel = render_kv_grid(
        output_review_summary,
        ["pair_count", "judgment_count", "pending_count", "agreement_count", "disagreement_count", "invalid_decision_count"],
        "review adjudication report missing",
    )
    conformance_panel = render_kv_grid(
        conformance_summary,
        ["target_count", "pass_count", "fail_count", "warning_count", "failure_count"],
        "runtime conformance matrix missing",
    )
    compiled_panel = render_kv_grid(
        compiled_summary,
        ["target_count", "pass_count", "warn_count", "block_count", "failure_count"],
        "compiled target report missing",
    )
    trust_panel = render_kv_grid(
        trust_summary,
        ["secret_findings", "script_count", "network_script_count", "help_smoke_failed_count", "package_sha256"],
        "security trust report missing",
    )
    runtime_boundary_panel = render_kv_grid(
        runtime_permissions_summary,
        ["target_count", "pass_count", "native_enforcement_count", "metadata_fallback_count", "residual_risk_count", "failure_count"],
        "runtime permission probe summary missing",
    )
    adoption_panel = render_kv_grid(
        adoption_summary,
        ["event_count", "adoption_rate", "missed_trigger_count", "bad_output_count", "risk_band"],
        "no adoption drift summary",
    )
    waiver_panel = render_kv_grid(
        waiver_summary,
        ["waiver_count", "active_count", "expired_count", "invalid_count", "covered_gate_count"],
        "no review waiver summary",
    )
    registry_panel = render_kv_grid(
        registry_package_summary(registry_package),
        [
            "name",
            "version",
            "maturity",
            "owner",
            "license",
            "trust_level",
            "targets",
            "compatibility_pass_count",
            "archive_sha256",
        ],
        "registry package metadata missing",
    )
    package_panel = render_kv_grid(
        package_summary,
        ["target_count", "adapter_count", "archive_present", "archive_entry_count", "failure_count", "warning_count", "archive_sha256"],
        "package verification missing",
    )
    evidence_html = "".join(
        f"<li><strong>{html.escape(key)}</strong><span>{html.escape(value)}</span></li>"
        for key, value in report["evidence_paths"].items()
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(str(title))} Review Studio 2.0</title>
  <style>
{review_studio_css()}
  </style>
</head>
<body>
  <nav>{nav_html}</nav>
  <main>
    <header id="overview">
      <div class="eyebrow">Review Studio 2.0</div>
      <h1>{html.escape(str(title))}</h1>
      <p class="lede">{html.escape(str(description))}</p>
      <div class="decision">
        <span>审查结论</span>
        <strong>{html.escape(summary['decision'])}</strong>
        <span>Score {html.escape(str(summary['world_class_score']))}/100</span>
      </div>
    </header>

    <section>
      <h2>核心指标</h2>
      <div class="metrics">{metrics_html}</div>
    </section>

    <section>
      <h2>审查闸门</h2>
      <div class="gates">{gates_html}</div>
    </section>

    <div class="twocol">
      {blockers_html}
      {warnings_html}
    </div>

    <section id="actions">
      <h2>修复动作</h2>
      <div class="actions-grid">{actions_html}</div>
    </section>

    <section id="annotations">
      <h2>审查批注</h2>
      <p class="muted">当前批注：{html.escape(annotation_caption)}</p>
      <div class="annotations-grid">{annotations_html}</div>
    </section>

    <section id="intent" class="twocol">
      <div class="panel">
        <h2>意图画布</h2>
        <p>{html.escape(str(report['data']['intent_confidence'].get('anchor_sentence', description)))}</p>
      </div>
      <div class="panel">
        <h2>证据路径</h2>
        <ul class="evidence">{evidence_html}</ul>
      </div>
    </section>

    <section id="trigger" class="twocol">
      <div class="panel"><h2>触发实验</h2><p>{html.escape(gates[1]['detail'])}</p></div>
      <div class="panel"><h2>组合治理</h2>{atlas_panel}</div>
    </section>

    <section id="output" class="twocol">
      <div class="panel"><h2>输出实验</h2>{output_panel}</div>
      <div class="panel"><h2>执行证据</h2>{execution_panel}</div>
    </section>

    <section class="twocol">
      <div class="panel"><h2>盲评包</h2>{blind_panel}</div>
      <div class="panel"><h2>审定报告</h2>{review_panel}</div>
    </section>

    <section class="twocol">
      <div class="panel"><h2>发布标准</h2><p>Governed 和 Library 至少需要 5 个 output eval cases，并覆盖 file-backed、near-neighbor、boundary case、execution evidence 和 blind A/B review pack。</p></div>
      <div class="panel"><h2>人工结论</h2><p>没有 reviewer 决策时只显示 pending；只有真实决策文件会进入一致率和分歧统计。</p></div>
    </section>

    <section class="twocol">
      <div class="panel"><h2>评审方式</h2><p>先看 reports/output_blind_review_pack.md 做盲评，填入 reports/output_review_decisions.json，再用 reports/output_review_adjudication.md 核对答案 key。</p></div>
      <div class="panel"><h2>运行方式</h2><p>reports/output_execution_runs.md 会标明 recorded fixture、command run 或 model run；只有 provider runner 返回 model metadata 时才算 model-executed。</p></div>
    </section>

    <section id="runtime" class="twocol">
      <div class="panel"><h2>运行矩阵</h2>{conformance_panel}</div>
      <div class="panel"><h2>目标编译</h2>{compiled_panel}</div>
    </section>

    <section class="twocol">
      <div class="panel"><h2>上下文</h2><p>{html.escape(gate_details.get('context-budget', 'context budget missing'))}</p></div>
      <div class="panel"><h2>编译证据</h2><p>Review reports/compiled_targets.md before packaging to inspect target adapter modes, generated files, preserved semantics, warnings, and unsupported features.</p></div>
    </section>

    <section id="trust" class="twocol">
      <div class="panel"><h2>信任报告</h2>{trust_panel}</div>
      <div class="panel"><h2>安全边界</h2><p>高风险 secret、远程 inline execution、缺失依赖策略或无法解释的脚本接口应阻断 governed release。</p></div>
    </section>

    <section id="permissions" class="twocol">
      <div class="panel"><h2>权限批准</h2><p>{html.escape(gate_details.get('permission-gates', 'permission governance missing'))}</p></div>
      <div class="panel"><h2>批准策略</h2><p>高权限能力需要 reviewer、scope、reason、expires_at 和 openai/claude/generic 目标端 enforcement 说明。</p></div>
    </section>

    <section id="permission-probes" class="twocol">
      <div class="panel"><h2>权限探针</h2><p>{html.escape(gate_details.get('permission-runtime', 'runtime permission probes missing'))}</p></div>
      <div class="panel"><h2>运行边界</h2>{runtime_boundary_panel}</div>
    </section>

    <section id="atlas" class="twocol">
      <div class="panel"><h2>组合治理</h2><p>{html.escape(gate_details.get('skill-atlas', 'skill atlas missing'))}</p></div>
      <div class="panel"><h2>下一动作</h2><p>优先处理真实 portfolio 中的 duplicate names、stale skills、owner gaps，再用运营回路判断真实影响。</p></div>
    </section>

    <section id="telemetry" class="twocol">
      <div class="panel"><h2>运营回路</h2><p>{html.escape(gate_details.get('operations-loop', 'adoption drift report missing'))}</p></div>
      <div class="panel"><h2>漂移信号</h2>{adoption_panel}</div>
    </section>

    <section id="waivers" class="twocol">
      <div class="panel"><h2>人工批准</h2><p>{html.escape(gate_details.get('review-waivers', 'review waiver ledger missing'))}</p></div>
      <div class="panel"><h2>批准台账</h2>{waiver_panel}</div>
    </section>

    <section id="registry" class="twocol">
      <div class="panel"><h2>注册审计</h2><p>{html.escape(gate_details.get('registry-audit', 'registry audit missing'))}</p></div>
      <div class="panel"><h2>包体元数据</h2>{registry_panel}</div>
    </section>

    <section id="release" class="twocol">
      <div class="panel"><h2>发布路线</h2><p>{html.escape(gate_details.get('release-notes', 'release notes missing'))}</p></div>
      <div class="panel"><h2>包体验证</h2>{package_panel}</div>
    </section>
  </main>
</body>
</html>
"""


def render_review_studio(skill_dir: Path, output_html: Path | None = None, output_json: Path | None = None) -> dict[str, Any]:
    skill_dir = skill_dir.resolve()
    reports_dir = skill_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_html = output_html or reports_dir / "review-studio.html"
    output_json = output_json or reports_dir / "review-studio.json"
    data = load_review_data(skill_dir)
    gates = build_gates(skill_dir, output_html, data)
    blockers, warnings = add_blockers_from_gate(gates)
    review_actions = build_review_actions(gates, skill_dir, output_html)
    score = weighted_score(gates)
    annotation_summary = data["review_annotations"].get("summary", {})
    open_annotation_blockers = int(annotation_summary.get("open_blocker_count", 0) or 0)
    open_annotation_warnings = int(annotation_summary.get("open_warning_count", 0) or 0)
    decision = "blocked" if blockers or open_annotation_blockers else ("review" if warnings or open_annotation_warnings else "ready")
    report = {
        "schema_version": "2.0",
        "ok": True,
        "skill_dir": display_path(skill_dir, skill_dir),
        "summary": {
            "decision": decision,
            "world_class_score": score,
            "gate_count": len(gates),
            "blocker_count": len(blockers),
            "warning_count": len(warnings),
            "action_count": len(review_actions),
            "annotation_count": int(annotation_summary.get("annotation_count", 0) or 0),
            "open_annotation_count": int(annotation_summary.get("open_count", 0) or 0),
            "open_annotation_blocker_count": open_annotation_blockers,
            "open_annotation_warning_count": open_annotation_warnings,
        },
        "gates": gates,
        "blockers": blockers,
        "warnings": warnings,
        "review_actions": review_actions,
        "evidence_paths": evidence_paths(skill_dir),
        "data": data,
        "artifacts": {
            "html": display_path(skill_dir, output_html),
            "json": display_path(skill_dir, output_json),
        },
    }
    output_html.write_text(render_html(report), encoding="utf-8")
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {key: value for key, value in report.items() if key != "data"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Render Review Studio 2.0 for a skill package.")
    parser.add_argument("skill_dir", nargs="?", default=".")
    parser.add_argument("--output-html")
    parser.add_argument("--output-json")
    args = parser.parse_args()
    payload = render_review_studio(
        Path(args.skill_dir),
        output_html=Path(args.output_html).resolve() if args.output_html else None,
        output_json=Path(args.output_json).resolve() if args.output_json else None,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
