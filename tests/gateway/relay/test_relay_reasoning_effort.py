from gateway.relay.ws_transport import _relay_metadata


def test_allowed_levels_cross_lowercased():
    for level in ("none", "minimal", "low", "medium", "high", "xhigh"):
        assert _relay_metadata({"reasoning_effort": level.upper()}) == {"reasoning_effort": level}


def test_costly_unknown_and_other_keys_dropped():
    for bad in ("max", "ultra", "hgih", "", None, {"effort": "low"}, 3):
        assert _relay_metadata({"reasoning_effort": bad}) == {}
    assert _relay_metadata({"gateway_session_key": "x", "reasoning_effort": "low"}) == {"reasoning_effort": "low"}
    assert _relay_metadata("low") == {}
