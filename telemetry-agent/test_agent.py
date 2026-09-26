from agent import LABELS, selected
from docker_api_proxy import allowed


def test_opt_in_labels_are_required_and_validated():
    labels = {LABELS["enabled"]: "true", LABELS["project"]: "billing",
              LABELS["environment"]: "staging", LABELS["commit"]: "a" * 40,
              LABELS["service"]: "web"}
    container = {"Id": "b" * 64, "ImageID": "sha256:" + "c" * 64, "Labels": labels}
    assert selected(container)["project_id"] == "billing"
    assert selected({**container, "Labels": {**labels, LABELS["enabled"]: "false"}}) is None
    assert selected({**container, "Labels": {k:v for k,v in labels.items() if k != LABELS["service"]}}) is None
    assert selected({**container, "Labels": {**labels, LABELS["commit"]: "short"}}) is None


def test_proxy_allows_only_read_inventory_events_and_health():
    assert allowed("GET", "/v1.41/containers/json?all=0")
    assert allowed("GET", "/v1.41/events?since=10")
    assert allowed("GET", "/_ping")
    assert allowed("GET", "/version")
    for method, path in [("POST", "/v1.41/containers/create"),
                         ("GET", "/v1.41/containers/abc/json"),
                         ("GET", "/v1.41/containers/abc/logs"),
                         ("GET", "/v1.41/containers/abc/archive"),
                         ("GET", "/v1.41/containers/abc/exec"),
                         ("GET", "/v1.41/images/json")]:
        assert not allowed(method, path)
