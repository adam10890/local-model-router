from __future__ import annotations

import pytest

from local_model_router.helpers.backends.base import BackendType
from local_model_router.helpers.backends.factory import create_backend, detect_backend


def test_detect_backend_is_native_first_unless_remote_hosts_are_declared():
    assert detect_backend({}) is BackendType.SUBPROCESS
    assert detect_backend({"lmm_hosts": {"chat": "router.test:8080"}}) is BackendType.REMOTE


def test_create_backend_honors_explicit_remote_and_subprocess():
    assert create_backend({}, "remote").backend_type is BackendType.REMOTE
    assert create_backend({}, BackendType.SUBPROCESS).backend_type is BackendType.SUBPROCESS


@pytest.mark.parametrize("configured", ["unknown", "", 7])
def test_unknown_explicit_backend_is_rejected(configured):
    with pytest.raises(ValueError, match="Unknown backend"):
        create_backend({"backend": configured})
