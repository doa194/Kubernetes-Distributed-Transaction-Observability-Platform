"""The automation must never change a cluster other than the local project cluster."""

import pytest

from txplatform import kube


def test_project_context_on_loopback_is_allowed():
    kube.check_context_is_local("kind-txplatform", "https://127.0.0.1:52341")


def test_other_context_is_refused():
    with pytest.raises(kube.ContextGuardError, match="only 'kind-txplatform'"):
        kube.check_context_is_local("production-eu", "https://127.0.0.1:6443")


def test_project_context_pointing_at_remote_server_is_refused():
    with pytest.raises(kube.ContextGuardError, match="not a local kind cluster"):
        kube.check_context_is_local("kind-txplatform", "https://10.20.30.40:6443")
