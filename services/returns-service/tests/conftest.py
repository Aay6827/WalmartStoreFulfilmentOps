import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def inventory():
    """Fake inventory-service. Flip state['mode'] to simulate failures."""
    state = {"mode": "ok", "calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"].append(json.loads(request.content))
        if state["mode"] == "down":
            raise httpx.ConnectError("inventory down", request=request)
        if state["mode"] == "5xx":
            return httpx.Response(503)
        if state["mode"] == "unknown":
            return httpx.Response(404, json={"detail": "unknown sku"})
        return httpx.Response(201, json={})

    state["client"] = httpx.Client(
        base_url="http://inventory.test", transport=httpx.MockTransport(handler)
    )
    return state


@pytest.fixture
def client(tmp_path, inventory):
    app = create_app(
        f"sqlite:///{(tmp_path / 'returns.db').as_posix()}",
        inventory_client=inventory["client"],
    )
    with TestClient(app) as c:
        yield c