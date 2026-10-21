"""The gateway's network ACL, kept from regressing silently.

Static checks on the compose file and the NetworkPolicy; whether a cluster
actually enforces the policy is proven by the probe in the policy's header.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    return yaml.safe_load((ROOT / name).read_text())


def _deployment(name):
    docs = yaml.safe_load_all((ROOT / name).read_text())

    return next(d for d in docs if d["kind"] == "Deployment")


def _networks(service):
    return set(service.get("networks") or ["default"])


def test_compose_gateway_is_private_to_the_api():
    services = _load("docker-compose.yml")["services"]
    gateway = services["llm_gateway"]

    assert not gateway.get("ports"), "gateway must not publish a host port"
    assert _networks(gateway) == {"llm"}

    on_llm = {name for name, svc in services.items()
              if "llm" in _networks(svc)}
    assert on_llm == {"llm_gateway", "api", "ollama"}


def test_k8s_policy_allows_only_the_api_on_8000():
    policy = _load("k8s/networkpolicy-llm-gateway.yaml")
    spec = policy["spec"]

    assert spec["podSelector"]["matchLabels"] == {"app": "llm-gateway"}
    assert spec["policyTypes"] == ["Ingress"]
    (rule,) = spec["ingress"]
    assert rule["from"] == [
        {"podSelector": {"matchLabels": {"app": "jobbed-api"}}}]
    assert [p["port"] for p in rule["ports"]] == [8000]


def test_k8s_labels_match_what_the_policy_selects():
    gateway = _deployment("k8s/llm-gateway.yaml")
    api = _deployment("k8s/api.yaml")

    def label(doc):
        return doc["spec"]["template"]["metadata"]["labels"]["app"]

    assert label(gateway) == "llm-gateway"
    assert label(api) == "jobbed-api"


def test_k8s_ollama_is_reachable_only_from_the_gateway():
    docs = list(yaml.safe_load_all((ROOT / "k8s/ollama.yaml").read_text()))
    policy = next(d for d in docs if d["kind"] == "NetworkPolicy")
    service = next(d for d in docs if d["kind"] == "Service")

    assert service["spec"].get("type", "ClusterIP") == "ClusterIP"
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app": "ollama"}
    (rule,) = policy["spec"]["ingress"]
    assert rule["from"] == [
        {"podSelector": {"matchLabels": {"app": "llm-gateway"}}}]
    assert policy["spec"]["policyTypes"] == ["Ingress"]


def test_k8s_api_runs_one_pod_at_a_time_with_its_caps():
    """Generations and in-flight locks are in memory: a rolling update would
    briefly run two pods and split them."""
    api = _deployment("k8s/api.yaml")
    env = {e["name"]: e.get("value")
           for e in api["spec"]["template"]["spec"]["containers"][0]["env"]}

    assert api["spec"]["strategy"] == {"type": "Recreate"}
    assert env["DAILY_CV_CAP"] == "2"
