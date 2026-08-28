from types import SimpleNamespace

from local_model_router.setup import hardware


def test_vulkan_and_nvidia_command_output_is_parsed_without_live_hardware(monkeypatch):
    monkeypatch.setattr(hardware, "_command_exists", lambda name: name == "vulkaninfo")

    vulkan = hardware._vulkan_devices(
        run=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="deviceName = Test GPU\ndeviceName = Test GPU\ndeviceName = Other GPU\n",
        )
    )
    driver = hardware._nvidia_driver(
        run=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="560.42\n")
    )

    assert vulkan == ["Test GPU", "Other GPU"]
    assert driver == "560.42"


def test_windows_adapter_output_is_normalized_without_powershell(monkeypatch):
    monkeypatch.setattr(hardware, "os", SimpleNamespace(name="nt"))
    payload = (
        '[{"Name":"Test GPU","AdapterRAM":4294967296,"DriverVersion":"1.2.3"},'
        '{"Name":"","AdapterRAM":0}]'
    )

    adapters = hardware._windows_adapters(
        run=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=payload)
    )

    assert adapters == [{
        "id": 0,
        "name": "Test GPU",
        "reported_graphics_memory_mb": 4096,
        "driver_version": "1.2.3",
        "source": "windows_cim",
        "confidence": "medium",
    }]
