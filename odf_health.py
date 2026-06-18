#!/usr/bin/env python3
"""
odf_health.py — Comprehensive health check for OpenShift Data Foundation (ODF).

Checks the health of all major ODF components and produces a colorized
summary report.  Components checked:

  - ODF Operator & Cluster versions
  - StorageCluster status
  - Ceph health (overall + OSDs + MONs + MGRs)
  - Ceph capacity / utilization
  - NooBaa / MCG status (core, backing stores, buckets)
  - NooBaa version mismatch (core vs. backing-store agents)
  - CNPG PostgreSQL cluster (NooBaa DB) — replica health
  - ODF-related pods (CrashLoopBackOff, Pending, Evicted)
  - ODF ClusterOperator status
  - PVs / StorageClasses

Configuration is loaded from (highest priority first):
  1. CLI flags (--ssh, --ssh-user, --ssh-key, --kubeconfig, --namespace)
  2. Environment variables (SSH_HOST, SSH_USER, SSH_KEY, KUBECONFIG, NAMESPACE)
  3. .env file next to the script or in the current working directory
  4. Defaults (see .env.example)

Usage:
  python3 odf_health.py                  # Full health check
  python3 odf_health.py --direct         # Direct cluster access (no SSH)
  python3 odf_health.py --quick          # Quick check (pods + Ceph only)
  python3 odf_health.py --json           # Output as JSON (for automation)
  python3 odf_health.py --namespace openshift-storage
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

# ── Configuration ────────────────────────────────────────────────────────────

NAMESPACE_DEFAULT = "openshift-storage"

COLORS = {
    "RED": "\033[91m",
    "GREEN": "\033[92m",
    "YELLOW": "\033[93m",
    "CYAN": "\033[96m",
    "BOLD": "\033[1m",
    "RESET": "\033[0m",
}

# Results collection for JSON output
results = {
    "timestamp": "",
    "namespace": "",
    "checks": {},
    "overall": "HEALTHY",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def color(text: str, color_name: str) -> str:
    return f"{COLORS[color_name]}{text}{COLORS['RESET']}"


def info(msg: str):
    print(color(f"ℹ  {msg}", "CYAN"))


def success(msg: str):
    print(color(f"✓  {msg}", "GREEN"))


def warn(msg: str):
    print(color(f"⚠  {msg}", "YELLOW"))


def error(msg: str):
    print(color(f"✗  {msg}", "RED"))


def section(title: str):
    print()
    print(color(f"{'━' * 60}", "BOLD"))
    print(color(f"  {title}", "BOLD"))
    print(color(f"{'━' * 60}", "BOLD"))


def run_cmd(cmd: list, check: bool = True, capture: bool = True, timeout: int = 60):
    """Run a local command."""
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)
    return subprocess.run(cmd, check=check, timeout=timeout)


# ── .env Loading ─────────────────────────────────────────────────────────────

def load_env(env_path: str) -> dict:
    """Load key=value pairs from a .env file."""
    env = {}
    path = Path(env_path)
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            value = value.strip().strip("\"'")
            env[key.strip()] = value
    return env


def resolve_config(args) -> tuple:
    """Resolve configuration from .env, env vars, CLI flags."""
    script_dir = Path(__file__).resolve().parent
    env_file = None
    candidates = []
    if args.env_file:
        candidates.append(Path(args.env_file))
    candidates.extend([script_dir / ".env", Path.cwd() / ".env"])
    for candidate in candidates:
        if candidate.exists():
            env_file = candidate
            break

    env_values = load_env(str(env_file)) if env_file else {}

    def resolve(cli_val, env_key, default=""):
        if cli_val is not None and cli_val != "":
            return cli_val
        if env_key in os.environ:
            return os.environ[env_key]
        if env_key in env_values:
            return env_values[env_key]
        return default

    config = {
        "SSH_HOST": resolve(args.ssh, "SSH_HOST", ""),
        "SSH_USER": resolve(args.ssh_user, "SSH_USER", ""),
        "SSH_KEY": resolve(args.ssh_key, "SSH_KEY", ""),
        "KUBECONFIG": resolve(args.kubeconfig, "KUBECONFIG", ""),
        "NAMESPACE": resolve(args.namespace, "NAMESPACE", NAMESPACE_DEFAULT),
    }
    return config, env_file


# ── oc Command Execution ─────────────────────────────────────────────────────

def oc(
    args: list,
    mode: str,
    ssh_host: str, ssh_user: str, ssh_key: str,
    namespace: str, kubeconfig: str,
    check: bool = True,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Dispatch oc command based on connection mode."""
    env = None
    if kubeconfig:
        kc_path = Path(kubeconfig).expanduser()
        if kc_path.exists():
            env = {**os.environ, "KUBECONFIG": str(kc_path)}

    if mode == "direct":
        cmd = ["oc", "-n", namespace] + args
        return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout, env=env)
    else:
        oc_args_str = " ".join(args)
        kc_path = Path(kubeconfig).expanduser() if kubeconfig else ""
        full_cmd = (
            f"ssh -i {ssh_key} {ssh_user}@{ssh_host} "
            f"'KUBECONFIG={kc_path} oc -n {namespace} {oc_args_str}'"
        )
        return subprocess.run(full_cmd, shell=True, capture_output=True, text=True, check=check, timeout=timeout)


def oc_get_json(path: str, mode: str, ssh_host: str, ssh_user: str, ssh_key: str, namespace: str, kubeconfig: str):
    """Run `oc get ... -o json` and return parsed JSON, or None on failure."""
    parts = path.split("/", 1)
    resource = parts[0]
    name = parts[1] if len(parts) > 1 else ""
    cmd_args = ["get", resource]
    if name:
        cmd_args.append(name)
    cmd_args.extend(["-o", "json"])
    try:
        result = oc(cmd_args, mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig, check=False)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return None


# ── Check Functions ──────────────────────────────────────────────────────────

def check_cluster_operators(mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig):
    """Check ODF-related ClusterOperator status."""
    section("CLUSTER OPERATORS")
    findings = []
    try:
        result = oc(["get", "co", "-o", "jsonpath={.items[?(@.spec.serviceAccountName=='ocs-operator')].metadata.name}"],
                     mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig, check=False)
        # Broader approach: get all COs and filter
        co_data = oc_get_json("clusterversions/version", mode, ssh_host, ssh_user, ssh_key, "", kubeconfig)
        if co_data and "status" in co_data and "conditions" in co_data.get("status", {}):
            for cond in co_data["status"]["conditions"]:
                if "odf" in cond.get("type", "").lower() or "ocs" in cond.get("type", "").lower():
                    status = "HEALTHY" if cond.get("status") == "True" else "DEGRADED"
                    msg = f"  {cond['type']}: {status}"
                    if status == "HEALTHY":
                        success(msg)
                    else:
                        warn(msg)
                    findings.append({"type": cond["type"], "status": status})
        # Fallback: check OCS/ODF operators
        for op in ["ocs-operator", "odf-operator"]:
            try:
                r = oc(["get", "deployment", op, "-o", "jsonpath={.status.readyReplicas}"],
                        mode, ssh_host, ssh_user, ssh_key, "openshift-storage", kubeconfig, check=False)
                if r.returncode == 0 and r.stdout.strip():
                    ready = r.stdout.strip()
                    spec = oc(["get", "deployment", op, "-o", "jsonpath={.spec.replicas}"],
                               mode, ssh_host, ssh_user, ssh_key, "openshift-storage", kubeconfig, check=False)
                    desired = spec.stdout.strip() if spec.returncode == 0 else "?"
                    if ready == desired and ready != "0":
                        success(f"  {op}: {ready}/{desired} ready")
                        findings.append({"type": op, "status": "HEALTHY"})
                    else:
                        warn(f"  {op}: {ready}/{desired} ready")
                        findings.append({"type": op, "status": "DEGRADED"})
            except Exception:
                pass
    except Exception as e:
        warn(f"  Could not check cluster operators: {e}")
        findings.append({"type": "cluster_operators", "status": "UNKNOWN", "error": str(e)})

    if not findings:
        info("  No ODF-specific cluster operators found (may be OCS-only deployment)")

    return findings


def check_storagecluster(mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig):
    """Check StorageCluster status."""
    section("STORAGECLUSTER")
    findings = []
    sc_data = oc_get_json("storagecluster", mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig)
    if not sc_data:
        warn("  Could not retrieve StorageCluster")
        findings.append({"type": "storagecluster", "status": "UNKNOWN"})
        return findings

    items = sc_data.get("items", [sc_data])  # handle List or single resource
    for sc in items:
        name = sc.get("metadata", {}).get("name", "unknown")
        phase = sc.get("status", {}).get("phase", "Unknown")
        msg = f"  {name}: phase={phase}"
        if phase == "Ready":
            success(msg)
            findings.append({"type": "storagecluster", "name": name, "status": "HEALTHY"})
        else:
            warn(msg)
            findings.append({"type": "storagecluster", "name": name, "status": "DEGRADED"})

        # Check conditions
        conditions = sc.get("status", {}).get("conditions", [])
        for cond in conditions:
            ctype = cond.get("type", "")
            cstatus = cond.get("status", "")
            if cstatus != "True" and ctype not in ("Progressing",):
                reason = cond.get("reason", "")
                msg2 = f"    Condition {ctype}: {cstatus} — {reason}"
                warn(msg2)
                findings.append({"type": f"sc_condition_{ctype}", "status": "DEGRADED", "reason": reason})

    return findings


def check_ceph_health(mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig):
    """Check Ceph cluster health."""
    section("CEPH HEALTH")
    findings = []

    # Get Ceph health from rook-ceph-mon pod
    try:
        # Find the mon pod
        mon_pods = oc(["get", "pods", "-l", "app=rook-ceph-mon", "-o", "jsonpath={.items[0].metadata.name}"],
                       mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig, check=False)
        if mon_pods.returncode != 0 or not mon_pods.stdout.strip():
            warn("  Could not find rook-ceph-mon pod")
            findings.append({"type": "ceph_health", "status": "UNKNOWN", "reason": "No mon pod found"})
            return findings

        mon_pod = mon_pods.stdout.strip()
        info(f"  Using monitor pod: {mon_pod}")

        # Run ceph status
        ceph_status = oc(
            ["exec", mon_pod, "--", "ceph", "status", "-f", "json"],
            mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig, check=False,
        )

        if ceph_status.returncode == 0:
            ceph = json.loads(ceph_status.stdout)

            # Overall health
            health = ceph.get("health", {}).get("overall", "UNKNOWN")
            findings.append({"type": "ceph_overall", "status": health})
            if health == "HEALTH_OK":
                success(f"  Overall: HEALTH_OK")
            elif health == "HEALTH_WARN":
                warn(f"  Overall: HEALTH_WARN")
            else:
                error(f"  Overall: {health}")

            # Detailed messages
            details = ceph.get("health", {}).get("detail", {})
            for msg_key in details:
                warn(f"  Warning: {msg_key}")
                findings.append({"type": f"ceph_warn_{msg_key.lower().replace(' ', '_')}", "status": "WARN"})

            # OSDs
            osds = ceph.get("fsid")  # just check we got data
            osdmap = ceph.get("osd", {})
            osd_up = osdmap.get("osds_up", 0)
            osd_in = osdmap.get("osds_in", 0)
            osd_exists = osdmap.get("osds", 0)
            success(f"  OSDs: {osd_up} up / {osd_in} in / {osd_exists} total")
            if osd_up < osd_in:
                warn(f"  Not all OSDs are up ({osd_up}/{osd_in})")
                findings.append({"type": "ceph_osds", "status": "DEGRADED", "detail": f"{osd_up}/{osd_in} up"})
            else:
                findings.append({"type": "ceph_osds", "status": "HEALTHY", "detail": f"{osd_up}/{osd_in} up"})

            # MONs
            mons = ceph.get("monmap", {}).get("mons", [])
            success(f"  MONs: {len(mons)} active")
            findings.append({"type": "ceph_mons", "status": "HEALTHY", "count": len(mons)})

            # MGRs
            mgrmap = ceph.get("mgrmap", {})
            active_mgr = mgrmap.get("standby_daemons", 0)
            success(f"  MGRs: 1 active + {active_mgr} standby")
            findings.append({"type": "ceph_mgrs", "status": "HEALTHY"})

            # Capacity
            ceph_statfs = oc(
                ["exec", mon_pod, "--", "ceph", "df", "-f", "json"],
                mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig, check=False,
            )
            if ceph_statfs.returncode == 0:
                df = json.loads(ceph_statfs.stdout)
                totals = df.get("stats", {}).get("total", {})
                total_bytes = totals.get("bytes", 0)
                used_bytes = totals.get("used", {}).get("raw", 0)
                if total_bytes > 0:
                    pct = (used_bytes / total_bytes) * 100
                    total_tb = total_bytes / (1024**4)
                    used_tb = used_bytes / (1024**4)
                    if pct > 80:
                        warn(f"  Capacity: {used_tb:.1f} TB / {total_tb:.1f} TB ({pct:.0f}%) — HIGH USAGE")
                        findings.append({"type": "ceph_capacity", "status": "WARN", "percent": pct})
                    else:
                        success(f"  Capacity: {used_tb:.1f} TB / {total_tb:.1f} TB ({pct:.0f}%)")
                        findings.append({"type": "ceph_capacity", "status": "HEALTHY", "percent": pct})

        else:
            error(f"  ceph status failed: {ceph_status.stderr.strip()}")
            findings.append({"type": "ceph_health", "status": "ERROR", "reason": ceph_status.stderr.strip()})

    except Exception as e:
        error(f"  Ceph health check failed: {e}")
        findings.append({"type": "ceph_health", "status": "ERROR", "reason": str(e)})

    return findings


def check_ceph_pods(mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig):
    """Check Ceph-related pod statuses."""
    section("CEPH PODS")
    findings = []

    try:
        result = oc([
            "get", "pods", "-l", "app=rook-ceph-mon",
            "-o", "custom-columns=NAME:.metadata.name,STATUS:.status.phase,RESTARTS:.status.containerStatuses[0].restartCount",
            "--no-headers"
        ], mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig)
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 3:
                name, status, restarts = parts[0], parts[1], parts[2]
                if status == "Running" and restarts == "0":
                    success(f"  {name}: {status} (restarts: {restarts})")
                    findings.append({"type": f"pod_{name}", "status": "HEALTHY"})
                elif status == "Running":
                    info(f"  {name}: {status} (restarts: {restarts})")
                    findings.append({"type": f"pod_{name}", "status": "HEALTHY", "restarts": int(restarts)})
                else:
                    error(f"  {name}: {status}")
                    findings.append({"type": f"pod_{name}", "status": "DEGRADED"})
    except Exception as e:
        warn(f"  Could not check MON pods: {e}")

    # Check OSD pods
    try:
        result = oc([
            "get", "pods", "-l", "app=rook-ceph-osd",
            "-o", "custom-columns=NAME:.metadata.name,STATUS:.status.phase,RESTARTS:.status.containerStatuses[0].restartCount",
            "--no-headers"
        ], mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig)
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 3:
                name, status, restarts = parts[0], parts[1], parts[2]
                if status == "Running" and restarts == "0":
                    success(f"  {name}: {status} (restarts: {restarts})")
                    findings.append({"type": f"pod_{name}", "status": "HEALTHY"})
                elif status == "Running":
                    info(f"  {name}: {status} (restarts: {restarts})")
                    findings.append({"type": f"pod_{name}", "status": "HEALTHY", "restarts": int(restarts)})
                else:
                    error(f"  {name}: {status}")
                    findings.append({"type": f"pod_{name}", "status": "DEGRADED"})
    except Exception as e:
        warn(f"  Could not check OSD pods: {e}")

    return findings


def check_nooobaa_status(mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig):
    """Check NooBaa / MCG component status."""
    section("NOOBAA / MCG")
    findings = []

    # NooBaa CR status
    try:
        nb_data = oc_get_json("noobaa", mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig)
        if nb_data:
            items = nb_data.get("items", [nb_data])
            for nb in items:
                name = nb.get("metadata", {}).get("name", "unknown")
                phase = nb.get("status", {}).get("phase", "Unknown")
                if phase == "Ready":
                    success(f"  NooBaa '{name}': {phase}")
                    findings.append({"type": "noobaa", "name": name, "status": "HEALTHY"})
                else:
                    warn(f"  NooBaa '{name}': {phase}")
                    findings.append({"type": "noobaa", "name": name, "status": "DEGRADED"})
    except Exception as e:
        warn(f"  Could not check NooBaa CR: {e}")

    # NooBaa pods
    try:
        result = oc([
            "get", "pods", "-l", "noobaa.io/noobaa",
            "-o", "custom-columns=NAME:.metadata.name,STATUS:.status.phase,RESTARTS:.status.containerStatuses[0].restartCount",
            "--no-headers"
        ], mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig, check=False)
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 3:
                    name, status, restarts = parts[0], parts[1], parts[2]
                    if status == "Running" and restarts == "0":
                        success(f"  {name}: {status}")
                        findings.append({"type": f"noobaa_{name}", "status": "HEALTHY"})
                    elif status == "Running":
                        warn(f"  {name}: {status} (restarts: {restarts})")
                        findings.append({"type": f"noobaa_{name}", "status": "WARN", "restarts": int(restarts)})
                    elif "CrashLoopBackOff" in status:
                        error(f"  {name}: CrashLoopBackOff")
                        findings.append({"type": f"noobaa_{name}", "status": "CRITICAL"})
                    else:
                        error(f"  {name}: {status}")
                        findings.append({"type": f"noobaa_{name}", "status": "DEGRADED"})
    except Exception as e:
        warn(f"  Could not check NooBaa pods: {e}")

    return findings


def check_version_mismatch(mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig):
    """Check for NooBaa core vs agent version mismatch."""
    section("NOOBAA VERSION CHECK")
    findings = []

    try:
        core_result = oc([
            "get", "sts/noobaa-core",
            "-o", "jsonpath={.spec.template.spec.containers[0].image}"
        ], mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig, check=False)

        if core_result.returncode != 0:
            warn("  Could not read noobaa-core image")
            findings.append({"type": "version_mismatch", "status": "UNKNOWN"})
            return findings

        core_image = core_result.stdout.strip()
        core_version = core_image.split("/")[-1].split("@")[0]

        # Get agent image
        agent_result = oc([
            "get", "pods", "-l", "backingstore-name=noobaa-default-backing-store",
            "-o", "jsonpath={.items[0].spec.containers[0].image}"
        ], mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig, check=False)

        if agent_result.returncode == 0 and agent_result.stdout.strip():
            agent_image = agent_result.stdout.strip()
            agent_version = agent_image.split("/")[-1].split("@")[0]

            info(f"  Core version:  {core_version}")
            info(f"  Agent version: {agent_version}")

            if core_version != agent_version:
                error(f"  VERSION MISMATCH — core ({core_version}) != agent ({agent_version})")
                findings.append({
                    "type": "version_mismatch", "status": "CRITICAL",
                    "detail": f"core={core_version} agent={agent_version}",
                })
            else:
                success("  Versions match")
                findings.append({"type": "version_mismatch", "status": "HEALTHY"})
        else:
            info("  No backing store agent pods found to compare (standalone MCG?)")
            findings.append({"type": "version_mismatch", "status": "UNKNOWN", "reason": "No agent pods"})

    except Exception as e:
        warn(f"  Version check failed: {e}")
        findings.append({"type": "version_mismatch", "status": "ERROR", "reason": str(e)})

    return findings


def check_pg_replica(mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig):
    """Check CNPG PostgreSQL replica health."""
    section("NOOBAA DATABASE (CNPG)")
    findings = []

    try:
        cluster_result = oc([
            "get", "clusters.postgresql.cnpg.noobaa.io", "noobaa-db-pg-cluster",
            "-o", "jsonpath={.status.instances},{.status.readyInstances},{.status.phase}"
        ], mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig, check=False)

        if cluster_result.returncode == 0:
            status = cluster_result.stdout.strip()
            parts = status.split(",")
            if len(parts) >= 3:
                instances = int(parts[0]) if parts[0].isdigit() else 0
                ready = int(parts[1]) if parts[1].isdigit() else 0
                phase = parts[2]

                info(f"  Cluster: {phase} ({ready}/{instances} ready)")

                if ready >= instances:
                    success(f"  All {ready}/{instances} instances ready")
                    findings.append({"type": "cnpg_replica", "status": "HEALTHY", "detail": f"{ready}/{instances} ready"})
                else:
                    warn(f"  Only {ready}/{instances} instances ready")
                    findings.append({"type": "cnpg_replica", "status": "DEGRADED", "detail": f"{ready}/{instances} ready"})
        else:
            warn("  Could not get CNPG cluster status")
            findings.append({"type": "cnpg_replica", "status": "UNKNOWN"})

        # Check for stuck replicas
        pods_result = oc([
            "get", "pods", "-l", "cluster-name=noobaa-db-pg-cluster", "--no-headers"
        ], mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig, check=False)

        if pods_result.returncode == 0:
            for line in pods_result.stdout.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 3:
                    name, ready_str, status = parts[0], parts[1], parts[2]
                    if "0/1" in ready_str and status == "Running":
                        warn(f"  {name}: {ready_str} {status} (potentially stuck)")
                        findings.append({"type": f"pg_{name}", "status": "WARN"})
                        # Check for pg_rewind issue
                        log_result = oc(
                            ["logs", name, "--tail=50"],
                            mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig, check=False,
                        )
                        if log_result.returncode == 0:
                            logs = log_result.stdout
                            if "pg_rewind" in logs and "common ancestor" in logs:
                                error(f"  {name}: pg_rewind 'no common ancestor' — needs manual resync")
                                findings.append({"type": f"pg_{name}", "status": "CRITICAL", "issue": "pg_rewind"})
                            elif "pg_rewind" in logs:
                                warn(f"  {name}: pg_rewind errors detected")
                                findings.append({"type": f"pg_{name}", "status": "WARN", "issue": "pg_rewind"})
                    elif status == "CrashLoopBackOff":
                        error(f"  {name}: CrashLoopBackOff")
                        findings.append({"type": f"pg_{name}", "status": "CRITICAL"})
                    elif status == "Running":
                        success(f"  {name}: {ready_str} {status}")
                        findings.append({"type": f"pg_{name}", "status": "HEALTHY"})
    except Exception as e:
        warn(f"  CNPG check failed: {e}")
        findings.append({"type": "cnpg_replica", "status": "ERROR", "reason": str(e)})

    return findings


def check_problematic_pods(mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig):
    """Check for CrashLoopBackOff, Pending, and Evicted pods in the ODF namespace."""
    section("POD STATUS OVERVIEW")
    findings = []

    unhealthy_statuses = {"CrashLoopBackOff", "Error", "Evicted", "ImagePullBackOff", "CreateContainerConfigError"}

    try:
        result = oc([
            "get", "pods",
            "-o", "custom-columns=NAME:.metadata.name,STATUS:.status.phase,RESTARTS:.status.containerStatuses[:1].restartCount,NODE:.spec.nodeName",
            "--no-headers"
        ], mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig)

        total = 0
        running = 0
        problematic = []

        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) < 2:
                continue
            total += 1
            name = parts[0]
            status = parts[1]

            # Extract the actual status (CrashLoopBackOff appears in the running state)
            # Check container statuses more carefully
            if status == "Running":
                running += 1
            elif status in unhealthy_statuses:
                problematic.append((name, status))
            elif status == "Pending":
                problematic.append((name, status))

        success(f"  Total pods: {total}")
        success(f"  Running: {running}")

        if problematic:
            error(f"  Problematic pods: {len(problematic)}")
            for name, status in problematic:
                error(f"    {name}: {status}")
                findings.append({"type": f"pod_{name}", "status": "CRITICAL", "reason": status})
        else:
            success("  No problematic pods")
            findings.append({"type": "pod_overview", "status": "HEALTHY"})

    except Exception as e:
        warn(f"  Pod check failed: {e}")
        findings.append({"type": "pod_overview", "status": "ERROR", "reason": str(e)})

    return findings


def check_storageclasses(mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig):
    """Check StorageClasses provisioned by ODF."""
    section("STORAGECLASSES")
    findings = []

    try:
        result = oc([
            "get", "storageclass",
            "-o", "custom-columns=NAME:.metadata.name,PROVISIONER:.provisioner,DEFAULT:.storageclass.kubernetes.io/is-default-class",
            "--no-headers"
        ], mode, ssh_host, ssh_user, ssh_key, "", kubeconfig, check=False)

        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 3:
                    name, provisioner, default = parts[0], parts[1], parts[2]
                    default_marker = " (default)" if default == "true" else ""
                    if "openshift-storage" in provisioner or "cephfs.csi.ceph.com" in provisioner or "ceph.rbd.csi.ceph.com" in provisioner:
                        success(f"  {name}{default_marker} — {provisioner}")
                        findings.append({"type": f"sc_{name}", "status": "HEALTHY"})
                    else:
                        info(f"  {name}{default_marker} — {provisioner}")
        else:
            warn("  Could not list StorageClasses")
            findings.append({"type": "storageclasses", "status": "UNKNOWN"})
    except Exception as e:
        warn(f"  StorageClass check failed: {e}")
        findings.append({"type": "storageclasses", "status": "ERROR", "reason": str(e)})

    return findings


def check_pv_health(mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig):
    """Check PersistentVolume health."""
    section("PERSISTENT VOLUMES")
    findings = []

    try:
        result = oc([
            "get", "pv",
            "-o", "custom-columns=NAME:.metadata.name,STATUS:.status.phase,CLAIM:.spec.claimRef.name",
            "--no-headers"
        ], mode, ssh_host, ssh_user, ssh_key, "", kubeconfig, check=False)

        if result.returncode == 0:
            total = 0
            bound = 0
            released = 0
            available = 0
            failed_pvs = []

            for line in result.stdout.strip().split("\n"):
                parts = line.split()
                if len(parts) < 2:
                    continue
                total += 1
                name = parts[0]
                status = parts[1]

                if status == "Bound":
                    bound += 1
                elif status == "Released":
                    released += 1
                elif status == "Available":
                    available += 1
                elif status in ("Failed", "Unknown"):
                    failed_pvs.append((name, status))

            info(f"  Total: {total}")
            success(f"  Bound: {bound}")
            if available:
                info(f"  Available: {available}")
            if released:
                warn(f"  Released: {released}")
            if failed_pvs:
                for name, status in failed_pvs:
                    error(f"  {name}: {status}")
                    findings.append({"type": f"pv_{name}", "status": "CRITICAL"})

            findings.append({"type": "pv_overview", "status": "HEALTHY" if not failed_pvs else "DEGRADED",
                           "detail": f"{bound} bound, {available} available, {released} released"})
        else:
            warn("  Could not list PVs")
            findings.append({"type": "pv_overview", "status": "UNKNOWN"})
    except Exception as e:
        warn(f"  PV check failed: {e}")
        findings.append({"type": "pv_overview", "status": "ERROR", "reason": str(e)})

    return findings


# ── Summary ──────────────────────────────────────────────────────────────────

def print_summary(all_findings: list, json_output: bool = False):
    """Print a summary of all checks."""
    section("SUMMARY")

    critical = [f for f in all_findings if f.get("status") == "CRITICAL"]
    degraded = [f for f in all_findings if f.get("status") in ("DEGRADED", "WARN")]
    healthy = [f for f in all_findings if f.get("status") == "HEALTHY"]
    unknown = [f for f in all_findings if f.get("status") in ("UNKNOWN", "ERROR")]

    if json_output:
        results["overall"] = "CRITICAL" if critical else "DEGRADED" if degraded else "HEALTHY"
        results["checks"] = {"critical": critical, "degraded": degraded, "healthy": healthy, "unknown": unknown}
        print(json.dumps(results, indent=2))
        return

    if critical:
        error(f"  CRITICAL: {len(critical)} issue(s)")
        for f in critical:
            error(f"    - {f.get('type', 'unknown')}: {f.get('reason', f.get('detail', 'see above'))}")
    else:
        success("  No critical issues")

    if degraded:
        warn(f"  WARNINGS: {len(degraded)} issue(s)")
        for f in degraded:
            warn(f"    - {f.get('type', 'unknown')}: {f.get('reason', f.get('detail', 'see above'))}")
    else:
        success("  No warnings")

    success(f"  Healthy: {len(healthy)} checks passed")

    if unknown:
        info(f"  Unknown: {len(unknown)} checks could not complete")

    overall = "CRITICAL" if critical else "DEGRADED" if degraded else "HEALTHY"
    print()
    status_color = {"CRITICAL": "RED", "DEGRADED": "YELLOW", "HEALTHY": "GREEN"}[overall]
    print(color(f"  OVERALL STATUS: {overall}", status_color))
    print()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="OpenShift Data Foundation (ODF) Health Check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Configuration priority (highest to lowest):
  1. CLI flags
  2. Environment variables (SSH_HOST, SSH_USER, SSH_KEY, KUBECONFIG, NAMESPACE)
  3. .env file (next to script or in cwd)
  4. Defaults (NAMESPACE=openshift-storage)

Examples:
  python3 odf_health.py                    # Full health check with .env
  python3 odf_health.py --direct           # Direct oc access
  python3 odf_health.py --quick            # Quick check (pods + Ceph only)
  python3 odf_health.py --json             # JSON output for automation
  python3 odf_health.py --ssh jump-host --ssh-user admin --ssh-key ~/.ssh/key
        """
    )

    parser.add_argument("--direct", action="store_true",
                        help="Connect directly using local oc + KUBECONFIG (no SSH)")
    parser.add_argument("--quick", action="store_true",
                        help="Quick check: only pods and Ceph health")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output results as JSON instead of colorized text")

    # SSH options
    parser.add_argument("--ssh", default=None, help="SSH jump host")
    parser.add_argument("--ssh-user", default=None, help="SSH username")
    parser.add_argument("--ssh-key", default=None, help="SSH private key path")

    # Connection options
    parser.add_argument("--kubeconfig", default=None, help="Path to kubeconfig file")
    parser.add_argument("--namespace", default=None, help="ODF namespace (default: openshift-storage)")
    parser.add_argument("--env-file", default=None, help="Path to .env file")

    args = parser.parse_args()
    config, env_file = resolve_config(args)

    mode = "direct" if args.direct else "ssh"
    ns = config["NAMESPACE"]
    results["timestamp"] = datetime.now(timezone.utc).isoformat()
    results["namespace"] = ns

    if not args.json_output:
        print(color(f"\n  OpenShift Data Foundation Health Check", "BOLD"))
        print(color(f"  Namespace: {ns}", "BOLD"))
        print(color(f"  Mode: {'Direct' if mode == 'direct' else 'SSH via ' + config['SSH_HOST']}", "BOLD"))
        if env_file:
            print(color(f"  Config: {env_file}", "BOLD"))
        print(color(f"{'━' * 60}", "BOLD"))

    all_findings = []

    try:
        if args.quick:
            # Quick mode: only pods and Ceph
            all_findings.extend(check_problematic_pods(mode, config["SSH_HOST"], config["SSH_USER"],
                                                       config["SSH_KEY"], ns, config["KUBECONFIG"]))
            all_findings.extend(check_ceph_health(mode, config["SSH_HOST"], config["SSH_USER"],
                                                  config["SSH_KEY"], ns, config["KUBECONFIG"]))
        else:
            # Full check
            all_findings.extend(check_cluster_operators(mode, config["SSH_HOST"], config["SSH_USER"],
                                                        config["SSH_KEY"], ns, config["KUBECONFIG"]))
            all_findings.extend(check_storagecluster(mode, config["SSH_HOST"], config["SSH_USER"],
                                                     config["SSH_KEY"], ns, config["KUBECONFIG"]))
            all_findings.extend(check_ceph_health(mode, config["SSH_HOST"], config["SSH_USER"],
                                                  config["SSH_KEY"], ns, config["KUBECONFIG"]))
            all_findings.extend(check_ceph_pods(mode, config["SSH_HOST"], config["SSH_USER"],
                                                config["SSH_KEY"], ns, config["KUBECONFIG"]))
            all_findings.extend(check_nooobaa_status(mode, config["SSH_HOST"], config["SSH_USER"],
                                                     config["SSH_KEY"], ns, config["KUBECONFIG"]))
            all_findings.extend(check_version_mismatch(mode, config["SSH_HOST"], config["SSH_USER"],
                                                       config["SSH_KEY"], ns, config["KUBECONFIG"]))
            all_findings.extend(check_pg_replica(mode, config["SSH_HOST"], config["SSH_USER"],
                                                 config["SSH_KEY"], ns, config["KUBECONFIG"]))
            all_findings.extend(check_problematic_pods(mode, config["SSH_HOST"], config["SSH_USER"],
                                                       config["SSH_KEY"], ns, config["KUBECONFIG"]))
            all_findings.extend(check_storageclasses(mode, config["SSH_HOST"], config["SSH_USER"],
                                                     config["SSH_KEY"], ns, config["KUBECONFIG"]))
            all_findings.extend(check_pv_health(mode, config["SSH_HOST"], config["SSH_USER"],
                                                config["SSH_KEY"], ns, config["KUBECONFIG"]))

    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)

    print_summary(all_findings, args.json_output)

    # Exit code based on findings
    critical = [f for f in all_findings if f.get("status") == "CRITICAL"]
    degraded = [f for f in all_findings if f.get("status") in ("DEGRADED", "WARN")]
    if critical:
        sys.exit(2)
    elif degraded:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
