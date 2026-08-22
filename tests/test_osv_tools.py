import httpx
import respx

from src.tools.osv_tools import (
    OSV_API_URL,
    OsvCheckUnavailable,
    check_package_vulnerabilities,
    infer_ecosystem,
)

import pytest


def test_infer_ecosystem_go_module_path():
    assert infer_ecosystem("k8s.io/client-go") == "Go"
    assert infer_ecosystem("github.com/sigstore/cosign/v3") == "Go"


def test_infer_ecosystem_github_action():
    assert infer_ecosystem("actions/checkout") == "GitHub Actions"


def test_infer_ecosystem_unknown_returns_none():
    assert infer_ecosystem("lodash") is None  # no "/" at all — not a shape we recognize here


@respx.mock
def test_check_package_vulnerabilities_clean():
    respx.post(OSV_API_URL).mock(return_value=httpx.Response(200, json={}))
    assert check_package_vulnerabilities("Go", "k8s.io/client-go", "0.31.1") == []


@respx.mock
def test_check_package_vulnerabilities_returns_matches():
    respx.post(OSV_API_URL).mock(
        return_value=httpx.Response(
            200,
            json={"vulns": [{"id": "GHSA-xxxx-xxxx-xxxx", "summary": "something bad"}, {"id": "GO-2024-1234"}]},
        )
    )
    vulns = check_package_vulnerabilities("Go", "k8s.io/client-go", "0.31.1")
    assert [v.id for v in vulns] == ["GHSA-xxxx-xxxx-xxxx", "GO-2024-1234"]
    assert vulns[0].summary == "something bad"
    assert vulns[1].summary is None


@respx.mock
def test_check_package_vulnerabilities_raises_on_http_error():
    respx.post(OSV_API_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(OsvCheckUnavailable):
        check_package_vulnerabilities("Go", "k8s.io/client-go", "0.31.1")


@respx.mock
def test_check_package_vulnerabilities_raises_on_timeout():
    respx.post(OSV_API_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(OsvCheckUnavailable):
        check_package_vulnerabilities("Go", "k8s.io/client-go", "0.31.1")
