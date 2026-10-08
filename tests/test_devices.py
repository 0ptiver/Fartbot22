"""Saved audio devices that Windows has renumbered (owner's case: input_device 3 became the
speakers after a headset came and went, and Nova wouldn't start at all)."""

from assistant.voice.audio import pick_device

DEVICES = [{"name": "Microsoft Sound Mapper", "max_input_channels": 2, "max_output_channels": 0},
           {"name": "Microphone Array (Realtek(R) Audio)", "max_input_channels": 2, "max_output_channels": 0},
           {"name": "Microphone (fifine SC3)", "max_input_channels": 1, "max_output_channels": 0},
           {"name": "Speakers (Realtek(R) Audio)", "max_input_channels": 0, "max_output_channels": 2}]
DEFAULT = {"input": 1, "output": 3}


def query(device=None, kind=None):
    if device is None:
        return DEVICES[DEFAULT[kind]]
    found = DEVICES[device] if isinstance(device, int) and device < len(DEVICES) else \
        next((d for d in DEVICES if isinstance(device, str) and device.lower() in d["name"].lower()), None)
    if found is None:
        raise ValueError(f"No device matching {device!r}")
    if kind and not found[f"max_{kind}_channels"]:
        raise ValueError(f"Not an {kind} device: {found['name']!r}")
    return found


def test_a_number_that_now_points_at_the_speakers_falls_back_to_the_default_mic():
    device, note = pick_device(3, "input", query)
    assert device is None
    assert "Speakers (Realtek(R) Audio)" in note and "Microphone Array" in note


def test_a_working_choice_is_kept():
    assert pick_device(2, "input", query) == (2, None)
    assert pick_device("fifine", "input", query) == ("fifine", None)
    assert pick_device(None, "input", query) == (None, None)


def test_an_unplugged_mic_by_name_falls_back():
    device, note = pick_device("Logitech PRO X", "input", query)
    assert device is None and "Logitech PRO X" in note
