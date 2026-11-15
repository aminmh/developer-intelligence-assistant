# The Docker proxy is Go; its tests are in proxy/main_test.go.
from agent import LABELS, negotiate_api_version, selected


def test_opt_in_labels_are_required_and_validated():
    labels = {LABELS["enabled"]: "true", LABELS["project"]: "billing",
              LABELS["environment"]: "staging", LABELS["commit"]: "a" * 40,
              LABELS["service"]: "web"}
    container = {"Id": "b" * 64, "ImageID": "sha256:" + "c" * 64, "Labels": labels}
    assert selected(container)["project_id"] == "billing"
    assert selected({**container, "Labels": {**labels, LABELS["enabled"]: "false"}}) is None
    assert selected({**container, "Labels": {k:v for k,v in labels.items() if k != LABELS["service"]}}) is None
    assert selected({**container, "Labels": {**labels, LABELS["commit"]: "short"}}) is None


def test_api_version_is_negotiated_within_the_engine_range():
    assert negotiate_api_version({"ApiVersion": "1.52", "MinAPIVersion": "1.44"}) == "v1.44"
    assert negotiate_api_version({"ApiVersion": "1.43", "MinAPIVersion": "1.24"}) == "v1.43"
    assert negotiate_api_version({"ApiVersion": "1.60", "MinAPIVersion": "1.50"}) == "v1.50"
    assert negotiate_api_version({}) == "v1.44"


def test_failure_reason_names_tls_and_http_errors_without_request_data():
    import ssl
    from urllib.error import HTTPError, URLError
    from agent import failure_reason
    tls = ssl.SSLCertVerificationError(1, "certificate verify failed")
    tls.verify_message = "Missing Authority Key Identifier"
    assert failure_reason(URLError(tls)) == "tls_verify:Missing_Authority_Key_Identifier"
    assert failure_reason(HTTPError("https://x/telemetry?secret=1", 403, "Forbidden", {}, None)) == "http_403"
    assert failure_reason(URLError(ConnectionRefusedError())) == "ConnectionRefusedError"
