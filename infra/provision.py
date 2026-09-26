"""Provision Skeleton Key's Vultr infrastructure.

Idempotent: every resource is looked up by label first and only created if
missing, so re-running is safe. IDs and IPs are written to infra/state.json;
secrets (Object Storage keys) are appended to .env, never to state.json.
"""
import base64
import json
import os
import pathlib
import ssl
import sys
import time
import urllib.error
import urllib.request

import certifi

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "infra" / "state.json"
ENV_FILE = ROOT / ".env"
API = "https://api.vultr.com/v2"
CTX = ssl.create_default_context(cafile=certifi.where())

REGION = "atl"  # same region as Vultr Serverless Inference
OS_UBUNTU_2404 = 2284
VPC_SUBNET, VPC_MASK = "10.10.0.0", 24
SSH_PUBKEY = pathlib.Path.home() / ".ssh" / "skeleton_key.pub"
OBJSTORE_CLUSTER_ATL2, OBJSTORE_TIER_STANDARD = 22, 2
# Container Registry has no Atlanta region; New Jersey is closest. Free plan.
REGISTRY_NAME, REGISTRY_REGION, REGISTRY_PLAN = "skeletonkey", "ewr", "start_up"
BROWSER_IMAGE, RUNNER_IMAGE = "sk-browser", "sk-runner"

INSTANCES = {
    "sk-control": {"plan": "vc2-2c-4gb", "firewall": "sk-control-fw", "role": "control"},
    "sk-worker-1": {"plan": "vhp-4c-8gb-intel", "firewall": "sk-worker-fw", "role": "worker"},
}
FIREWALLS = {
    # Public-interface rules only; VPC traffic is filtered by ufw on each host.
    "sk-control-fw": [22, 80, 443],
    "sk-worker-fw": [22],
}


def load_env():
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def api(method, path, body=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": "Bearer " + os.environ["VULTR_API_KEY"],
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> {e.code}: {e.read().decode()[:400]}")


def find(path, key, match):
    items = api("GET", f"{path}?per_page=500").get(key, [])
    return next((i for i in items if match(i)), None)


def registry_credentials(registry_id, read_write=False, expiry_seconds=0):
    """Vultr issues docker credentials via OPTIONS; returns (host, user, password)."""
    auths = api("OPTIONS", f"/registry/{registry_id}/docker-credentials"
                           f"?expiry_seconds={expiry_seconds}&read_write={str(read_write).lower()}")["auths"]
    host, entry = next(iter(auths.items()))
    user, password = base64.b64decode(entry["auth"]).decode().split(":", 1)
    return host, user, password


def bootstrap_script(role, pull_creds=None):
    """Boot script stored as a Vultr Startup Script (one per role)."""
    ufw = (
        "ufw allow 80/tcp\nufw allow 443/tcp\nufw allow from 10.10.0.0/24\n"
        if role == "control"
        else "ufw allow from 10.10.0.0/24\n"
    )
    pull = ""
    if role == "worker" and pull_creds:
        host, user, password = pull_creds
        # Read-only credentials: a compromised worker cannot overwrite images.
        pull = f"echo '{password}' | docker login {host} -u '{user}' --password-stdin\n"
        for image in (BROWSER_IMAGE, RUNNER_IMAGE):
            pull += (f"docker pull {host}/{REGISTRY_NAME}/{image}:latest\n"
                     f"docker tag {host}/{REGISTRY_NAME}/{image}:latest {image}:latest\n")
        # Same rules as worker/harden.sh: containers can't reach the VPC or the metadata service.
        pull += ("iptables -I DOCKER-USER -d 10.10.0.0/24 -m conntrack --ctstate NEW -j DROP\n"
                 "iptables -I DOCKER-USER -d 169.254.169.254/32 -j DROP\n")
    return f"""#!/bin/bash
set -eux
export DEBIAN_FRONTEND=noninteractive
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
ufw allow 22/tcp
{ufw}ufw --force enable
{pull}touch /root/sk-bootstrap-done
"""


def main():
    load_env()
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    key = find("/ssh-keys", "ssh_keys", lambda k: k["name"] == "skeleton-key")
    if not key:
        key = api("POST", "/ssh-keys", {"name": "skeleton-key", "ssh_key": SSH_PUBKEY.read_text().strip()})["ssh_key"]
    state["ssh_key_id"] = key["id"]

    vpc = find("/vpcs", "vpcs", lambda v: v["description"] == "sk-vpc")
    if not vpc:
        vpc = api("POST", "/vpcs", {"region": REGION, "description": "sk-vpc",
                                     "v4_subnet": VPC_SUBNET, "v4_subnet_mask": VPC_MASK})["vpc"]
    state["vpc_id"] = vpc["id"]

    state.setdefault("firewalls", {})
    for name, ports in FIREWALLS.items():
        fw = find("/firewalls", "firewall_groups", lambda f: f["description"] == name)
        if not fw:
            fw = api("POST", "/firewalls", {"description": name})["firewall_group"]
            for port in ports:
                api("POST", f"/firewalls/{fw['id']}/rules", {"ip_type": "v4", "protocol": "tcp", "subnet": "0.0.0.0", "subnet_size": 0, "port": str(port), "notes": name})
                api("POST", f"/firewalls/{fw['id']}/rules", {"ip_type": "v6", "protocol": "tcp", "subnet": "::", "subnet_size": 0, "port": str(port), "notes": name})
        state["firewalls"][name] = fw["id"]

    reg = find("/registries", "registries", lambda r: r["name"] == REGISTRY_NAME)
    if not reg:
        reg = api("POST", "/registry", {"name": REGISTRY_NAME, "public": False,
                                         "region": REGISTRY_REGION, "plan": REGISTRY_PLAN})
        reg = reg.get("registry", reg)
    state["registry"] = {"id": reg["id"], "urn": reg["urn"], "name": REGISTRY_NAME}

    # One Startup Script per role; updated in place so the script always matches this file.
    pull_creds = registry_credentials(reg["id"], read_write=False)
    state.setdefault("startup_scripts", {})
    for role in ("control", "worker"):
        name = f"sk-bootstrap-{role}"
        body = {"name": name, "type": "boot",
                "script": base64.b64encode(bootstrap_script(role, pull_creds).encode()).decode()}
        script = find("/startup-scripts", "startup_scripts", lambda s: s["name"] == name)
        if script:
            api("PATCH", f"/startup-scripts/{script['id']}", body)
        else:
            script = api("POST", "/startup-scripts", body)["startup_script"]
        state["startup_scripts"][role] = script["id"]

    state.setdefault("instances", {})
    for label, spec in INSTANCES.items():
        inst = find("/instances", "instances", lambda i: i["label"] == label)
        if not inst:
            inst = api("POST", "/instances", {
                "region": REGION, "plan": spec["plan"], "os_id": OS_UBUNTU_2404,
                "label": label, "hostname": label, "sshkey_id": [key["id"]],
                "firewall_group_id": state["firewalls"][spec["firewall"]],
                "attach_vpc": [vpc["id"]], "enable_ipv6": True, "backups": "disabled",
                "script_id": state["startup_scripts"][spec["role"]],
                "tags": ["skeleton-key", spec["role"]],
            })["instance"]
        state["instances"][label] = {"id": inst["id"], "role": spec["role"], "plan": spec["plan"]}

    store = find("/object-storage", "object_storages", lambda o: o["label"] == "sk-store")
    if not store:
        store = api("POST", "/object-storage", {"cluster_id": OBJSTORE_CLUSTER_ATL2,
                                                 "tier_id": OBJSTORE_TIER_STANDARD, "label": "sk-store"})["object_storage"]
    state["object_storage_id"] = store["id"]

    # Wait for instances to get IPs and for object storage keys.
    for _ in range(60):
        pending = False
        for label, meta in state["instances"].items():
            i = api("GET", f"/instances/{meta['id']}")["instance"]
            meta.update(ip=i["main_ip"], ipv6=i["v6_main_ip"], status=i["status"], power=i["power_status"])
            if i["main_ip"] in ("", "0.0.0.0") or i["status"] != "active":
                pending = True
            else:
                vpcs = api("GET", f"/instances/{meta['id']}/vpcs").get("vpcs", [])
                meta["vpc_ip"] = vpcs[0]["ip_address"] if vpcs else None
        s = api("GET", f"/object-storage/{state['object_storage_id']}")["object_storage"]
        if s["status"] != "active":
            pending = True
        if not pending:
            break
        print("waiting for resources...", {k: v.get("status") for k, v in state["instances"].items()}, "store:", s["status"])
        time.sleep(10)

    state["object_storage"] = {"hostname": s["s3_hostname"], "status": s["status"]}
    env = ENV_FILE.read_text()
    if "S3_ACCESS_KEY" not in env and s.get("s3_access_key"):
        with ENV_FILE.open("a") as f:
            f.write(("" if env.endswith("\n") else "\n") +
                    f"S3_ENDPOINT=https://{s['s3_hostname']}\nS3_ACCESS_KEY={s['s3_access_key']}\nS3_SECRET_KEY={s['s3_secret_key']}\n")

    STATE_FILE.write_text(json.dumps(state, indent=2))
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
