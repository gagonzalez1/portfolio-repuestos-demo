import asyncio
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app import web_demo
from app.main import health


def fake_settings(**overrides):
    values = {
        "demo_enabled": True,
        "demo_session_ttl_seconds": 3600,
        "demo_max_messages_per_session": 12,
        "demo_max_messages_per_ip_day": 30,
        "demo_global_messages_per_day": 500,
        "demo_max_message_length": 1000,
        "demo_trust_forwarded_for": False,
        "secure_cookies": False,
        "metaia_cta_url": "https://metaia.pro/",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class PortfolioRoutesTests(unittest.TestCase):
    def setUp(self):
        web_demo._visitor_sessions.clear()
        web_demo._daily_usage.update({"day": date.today(), "global": 0, "ips": {}})
        app = FastAPI()
        app.include_router(web_demo.router)
        self.settings_patch = patch("app.web_demo.get_settings", return_value=fake_settings())
        self.settings_patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.settings_patch.stop()

    def test_public_landing_is_fictitious_and_has_no_client_brand(self):
        response = self.client.get("/demo")
        self.assertEqual(response.status_code, 200)
        self.assertIn("productos, precios y disponibilidad son ficticios", response.text)
        self.assertNotIn("Aurelia", response.text)
        self.assertIn("MetaIA", response.text)

    def test_anonymous_session_and_new_conversation(self):
        started = self.client.post("/demo/api/session")
        self.assertEqual(started.status_code, 200)
        first_alias = self.client.get("/demo/api/me").json()["alias"]
        renewed = self.client.post("/demo/api/new-conversation")
        self.assertEqual(renewed.status_code, 200)
        second_alias = self.client.get("/demo/api/me").json()["alias"]
        self.assertNotEqual(first_alias, second_alias)

    def test_chat_presents_rich_editable_example_cases(self):
        self.client.post("/demo/api/session")
        response = self.client.get("/demo/chat")

        self.assertEqual(response.status_code, 200)
        self.assertIn("¿Querés ver cómo razona MotorIA?", response.text)
        self.assertIn("Podés editar el mensaje antes de enviarlo", response.text)
        self.assertEqual(response.text.count('class="suggestion" data-prompt='), 5)
        self.assertIn("Caso 2 · Motor y medida", response.text)
        self.assertIn("aros Chevrolet Corsa 1.8 STD", response.text)
        self.assertIn("30 x 47 x 7 mm", response.text)

    def test_admin_dashboard_redirects_without_private_session(self):
        response = self.client.get("/admin/dashboard", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/admin")


class QuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        web_demo._daily_usage.update({"day": date.today(), "global": 0, "ips": {}})
        self.session = {
            "message_count": 0,
            "client_key": "hashed-ip",
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        }

    async def test_session_limit_blocks_before_another_llm_call(self):
        settings = fake_settings(demo_max_messages_per_session=1)
        with patch("app.web_demo.get_settings", return_value=settings):
            await web_demo._consume_quota(self.session)
            with self.assertRaisesRegex(Exception, "límite de esta conversación"):
                await web_demo._consume_quota(self.session)
        self.assertEqual(web_demo._daily_usage["global"], 1)

    async def test_health_is_a_pure_liveness_response(self):
        self.assertEqual(
            await health(),
            {"status": "ok", "service": "portfolio-repuestos-demo"},
        )


class SettingsTests(unittest.TestCase):
    def test_external_credentials_are_optional_and_extra_is_ignored(self):
        settings = Settings(
            _env_file=None,
            database_url="postgresql://demo",
            meta_access_token="",
            unknown_deploy_var="ignored",
        )
        self.assertFalse(settings.whatsapp_enabled)
        self.assertEqual(settings.meta_access_token, "")
        self.assertEqual(settings.catalog_backend, "demo_postgres")


if __name__ == "__main__":
    unittest.main()
