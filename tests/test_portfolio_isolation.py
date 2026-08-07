"""Garantías del runtime aislado de portfolio."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import patch

from app import bootstrap, main


def test_health_is_static_and_does_not_require_initialized_dependencies():
    assert asyncio.run(main.health()) == {
        "status": "ok",
        "service": "portfolio-repuestos-demo",
    }


def test_meta_webhook_is_not_available_when_channel_is_disabled():
    with patch.object(main, "get_settings", return_value=SimpleNamespace(whatsapp_enabled=False)):
        response = asyncio.run(main.verify_webhook())
    assert response.status_code == 404


def test_bootstrap_has_no_http_or_commerce_client_execution_path():
    source = inspect.getsource(bootstrap)
    assert "import httpx" not in source
    assert "WooCommerceClient" not in source
    assert "build_motor_expand_from_wc" not in source
    assert 'settings.catalog_backend != "demo_postgres"' in source
