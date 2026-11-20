"""Linux machine facts must work without macOS IOKit/ctypes dependencies."""
import pytest


@pytest.mark.platforms("linux")
def test_linux_host_identity_without_native_telemetry():
    from hermes_platform.host import facts
    assert facts.os_family() == "linux"
