"""Unit tests must explicitly provide inference doubles, including worker factories."""

import pytest


@pytest.fixture(autouse=True)
def no_live_worker_inference(monkeypatch):
    def blocked(settings):
        raise AssertionError("Unit test must supply a worker provider factory; live inference is disabled")

    monkeypatch.setattr("datasheet_parser.engine.make_provider", blocked)
