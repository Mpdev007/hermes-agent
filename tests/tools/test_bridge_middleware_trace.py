"""Successful target middleware preserves wrapper trace just like fallback delivery."""
from types import SimpleNamespace


def test_target_middleware_keeps_prior_wrapper_observation(monkeypatch):
    import model_tools
    from hermes_cli import middleware
    monkeypatch.setattr(middleware, "apply_tool_request_middleware", lambda *_a, **_k:
                        SimpleNamespace(payload={"changed": True}, original_payload={},
                                        trace=[{"stage": "target"}]))
    prior = [{"stage": "wrapper"}]
    args, _, trace = model_tools._apply_request_middleware("fixture", {}, model_tools._CallIds(), prior)
    assert args == {"changed": True}
    assert trace == [{"stage": "wrapper"}, {"stage": "target"}]
    assert prior == [{"stage": "wrapper"}]
