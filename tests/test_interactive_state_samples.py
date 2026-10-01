from core.dynamic_validation import interactive_control_specs


def test_interactive_control_specs_is_explicit_and_bounded():
    nodes = [
        {"index": 1, "tag": "input", "type": "checkbox", "attrs": {"id": "alarm"}},
        {"index": 2, "tag": "div", "role": "switch", "attrs": {"data-openhmi-id": "pump"}},
        {"index": 3, "tag": "button", "attrs": {"id": "mode", "aria-pressed": "false"}},
        # Text alone is not a stable trigger identity.
        {"index": 4, "tag": "button", "text": "Do not discover", "attrs": {}},
        {"index": 5, "tag": "input", "type": "radio", "attrs": {"id": "radio"}},
    ]
    assert interactive_control_specs(nodes) == [
        {"identity": "alarm", "kind": "checkbox", "index": 1},
        {"identity": "pump", "kind": "switch", "index": 2},
        {"identity": "mode", "kind": "button-pressed", "index": 3},
    ]
    assert len(interactive_control_specs(nodes, limit=2)) == 2


def test_interactive_control_specs_does_not_use_index_as_identity():
    assert interactive_control_specs([
        {"index": 0, "tag": "input", "type": "checkbox", "attrs": {}},
        {"index": 1, "tag": "div", "role": "switch", "attrs": {}},
    ]) == []
