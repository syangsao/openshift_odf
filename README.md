# openshift_odf

Comprehensive health check tool for OpenShift Data Foundation (ODF).

`odf_health.py` connects to your OpenShift cluster and checks the health of all major ODF components — Ceph, NooBaa/MCG, CNPG PostgreSQL, StorageClasses, and more — producing a colorized report or structured JSON output.

## Why

ODF is a complex system with many moving parts. When something degrades, the root cause can be in Ceph OSDs, NooBaa version mismatches, stuck PostgreSQL replicas, or operator issues. Instead of running a dozen `oc` commands and checking multiple dashboards, this script gives you a single view of ODF health.

## What It Checks

| Check | What it looks for |
|-------|------------------|
| **Cluster Operators** | OCS/ODF operator deployments, ready replicas |
| **StorageCluster** | StorageCluster CR phase (`Ready`, `Implementing`, `Preparing`, errors) |
| **Ceph Health** | `ceph status` overall state, OSD/MON/MGR counts, capacity utilization |
| **Ceph Pods** | MON and OSD pod statuses, restart counts |
| **NooBaa / MCG** | NooBaa CR phase, pod statuses (core, DB, backing store agents) |
| **Version Mismatch** | Compares `noobaa-core` image version vs. backing store agent version — a known issue that causes CrashLoopBackOff after ODF upgrades |
| **CNPG PostgreSQL** | NooBaa database cluster status, replica readiness, `pg_rewind` errors (stuck replicas with no common WAL ancestor) |
| **Pod Overview** | All pods in the ODF namespace — flags CrashLoopBackOff, Error, Evicted, Pending |
| **StorageClasses** | Lists ODF-provisioned StorageClasses and the default |
| **Persistent Volumes** | PV counts (Bound, Available, Released, Failed) |

## Quick Start

```bash
# Clone the repo
git clone https://github.com/syangsao/openshift_odf.git
cd openshift_odf

# Configure
cp .env.example .env
# Edit .env with your SSH host, kubeconfig path, and namespace

# Run a full health check
python3 odf_health.py

# Quick check (pods + Ceph only)
python3 odf_health.py --quick
```

### Output Example

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  OpenShift Data Foundation Health Check
  Namespace: openshift-storage
  Mode: SSH via jump-host.example.com
  Config: /path/to/.env
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CEPH HEALTH
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ℹ  Using monitor pod: rook-ceph-mon-a-abc123
✓  Overall: HEALTH_OK
✓  OSDs: 9 up / 9 in / 9 total
✓  MONs: 3 active
✓  MGRs: 1 active + 1 standby
✓  Capacity: 4.2 TB / 22.4 TB (19%)

...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✓  No critical issues
✓  No warnings
✓  Healthy: 14 checks passed

  OVERALL STATUS: HEALTHY
```

## Usage

```bash
# Full health check (uses .env for config)
python3 odf_health.py

# Direct cluster access (oc already configured locally)
python3 odf_health.py --direct

# Quick check — pods and Ceph only (faster)
python3 odf_health.py --quick

# JSON output (for automation / CI / monitoring)
python3 odf_health.py --json

# Override config via CLI flags
python3 odf_health.py --ssh jump-host --ssh-user admin --ssh-key ~/.ssh/id_ed25519
python3 odf_health.py --namespace openshift-storage --kubeconfig ~/.kube/config

# Specify a custom .env file
python3 odf_health.py --env-file /path/to/custom.env
```

### Command-Line Options

| Flag | Description | Default |
|------|-------------|---------|
| `--direct` | Connect directly using local `oc` + `KUBECONFIG` | SSH mode |
| `--quick` | Quick check (pods + Ceph health only) | Full check |
| `--json` | Output as JSON instead of colorized text | Colorized |
| `--ssh HOST` | SSH jump host | From `.env` |
| `--ssh-user USER` | SSH username | From `.env` |
| `--ssh-key PATH` | SSH private key path | From `.env` |
| `--kubeconfig PATH` | Path to kubeconfig file | From `.env` |
| `--namespace NS` | ODF namespace | `openshift-storage` |
| `--env-file PATH` | Path to `.env` file | Script dir or cwd |

### Configuration

The script loads configuration from three sources in priority order:

1. **CLI flags** (highest priority)
2. **Environment variables** (`SSH_HOST`, `SSH_USER`, `SSH_KEY`, `KUBECONFIG`, `NAMESPACE`)
3. **`.env` file** (next to the script or in the current working directory)
4. **Defaults** (`NAMESPACE=openshift-storage`)

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

### Connection Modes

**SSH through jump host** (default when `--direct` is not set):

The script SSHs into your jump host and runs `oc` commands there. The kubeconfig path in `.env` should be the path on the jump host.

```bash
# .env
SSH_HOST=jump-host.example.com
SSH_USER=admin
SSH_KEY=~/.ssh/id_ed25519
KUBECONFIG=~/ocp-cluster/auth/kubeconfig
```

**Direct access** (`--direct`):

Uses the local `oc` CLI and kubeconfig. Useful when you already have `oc` configured on the machine running the script.

```bash
python3 odf_health.py --direct --kubeconfig ~/.kube/config
```

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | All checks passed (HEALTHY) |
| `1` | Warnings detected (DEGRADED) |
| `2` | Critical issues found (CRITICAL) |

This makes the script suitable for CI/CD pipelines or monitoring systems.

### JSON Output

The `--json` flag produces structured output for automation:

```json
{
  "timestamp": "2026-06-18T10:30:00+00:00",
  "namespace": "openshift-storage",
  "overall": "HEALTHY",
  "checks": {
    "healthy": [
      {"type": "ceph_overall", "status": "HEALTH_OK"},
      {"type": "ceph_osds", "status": "HEALTHY", "detail": "9/9 up"},
      ...
    ],
    "degraded": [],
    "critical": [],
    "unknown": []
  }
}
```

## Known Issues Detected

### NooBaa Version Mismatch

After an ODF operator upgrade, backing store agent pods can enter `CrashLoopBackOff` because the `noobaa-core` StatefulSet runs an older `mcg-core-rhel9` image while agents have been upgraded. The operator updates `spec.image` on the NooBaa CR but doesn't reconcile the StatefulSet automatically.

**Detection:** The script compares `noobaa-core` image version against backing store agent image version.

**Fix:** See [openshift_noobaa](https://github.com/syangsao/openshift_noobaa) for the automated repair script.

### CNPG PostgreSQL Stuck Replica

CloudNative PostgreSQL replicas can get stuck in a `pg_rewind` loop when there's no common WAL ancestor between source and target. CNPG doesn't fall back to `pg_basebackup` in this scenario, so the replica stays at `0/1 Running` indefinitely.

**Detection:** The script checks for `0/1 Running` pods and scans logs for `pg_rewind` / `common ancestor` errors.

**Fix:** See [openshift_noobaa](https://github.com/syangsao/openshift_noobaa) for the automated repair script.

## Requirements

- Python 3.10+ (uses `list[str]` type hints)
- `oc` CLI available (locally for `--direct`, or on the jump host for SSH mode)
- SSH access to jump host (when not using `--direct`)
- Cluster permissions to read pods, StatefulSets, CustomResources, and execute into pods (`system:admin` or equivalent)

## Integrating with Monitoring

### Cron Job

```bash
# Run every 6 hours, email on failure
0 */6 * * * cd /path/to/openshift_odf && python3 odf_health.py --direct 2>&1 | mail -s "ODF Health Check" admin@example.com
```

### CI Pipeline

```yaml
- name: Check ODF Health
  run: |
    cp .env.example .env
    # Set env vars or use secrets for SSH_HOST, SSH_KEY, etc.
    python3 odf_health.py --json > /tmp/odf_health.json
    python3 odf_health.py  # Exit code 0=healthy, 1=degraded, 2=critical
```

## Related Projects

- [openshift_noobaa](https://github.com/syangsao/openshift_noobaa) — Automated repair tool for NooBaa CrashLoopBackOff and CNPG replica issues

## License

MIT
