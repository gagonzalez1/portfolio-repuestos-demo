from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración del runtime tradicional y del modo portfolio.

    Las credenciales de canales externos son opcionales para que la demo pueda
    arrancar sin secretos ficticios. Los componentes que las usan deben validar
    primero su correspondiente flag de habilitación.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Meta Cloud API (deshabilitada por defecto en portfolio)
    whatsapp_enabled: bool = False
    meta_verify_token: str = ""
    meta_access_token: str = ""
    meta_phone_number_id: str = ""
    meta_api_version: str = "v22.0"

    # LLM
    gemini_api_key: str = ""
    llm_api_key: str = ""  # alias conceptual para despliegues nuevos
    llm_model: str = "gemini/gemini-2.5-flash"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 1024

    # Whisper STT
    openai_api_key: str = ""

    # Catálogo. Las credenciales Woo quedan opcionales en el backend demo.
    catalog_backend: str = "demo_postgres"
    wc_store_url: str = ""
    wc_consumer_key: str = ""
    wc_consumer_secret: str = ""

    # PostgreSQL
    database_url: str

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    webhook_path: str = "/webhook"
    port: int = 8000

    # Identidad y CTA del portfolio
    portfolio_name: str = "MotorIA Demo"
    metaia_cta_url: str = "https://metaia.pro/"

    # Controles de la demo pública
    demo_enabled: bool = True
    demo_max_messages_per_session: int = Field(default=12, ge=1, le=100)
    demo_max_messages_per_ip_day: int = Field(default=30, ge=1, le=1000)
    demo_global_messages_per_day: int = Field(default=500, ge=1, le=100000)
    demo_max_message_length: int = Field(default=1000, ge=50, le=5000)
    demo_session_ttl_seconds: int = Field(default=3600, ge=300, le=86400)
    demo_retention_days: int = Field(default=7, ge=1, le=30)
    demo_trust_forwarded_for: bool = False

    # Escalado legado (vacío en portfolio; la UI usa el CTA de MetaIA)
    human_escalation_phone: str = ""

    # Refresh solo aplica al backend WooCommerce.
    motor_expand_refresh_hours: int = 0

    # La demo pública no requiere clave. Estos secretos protegen solamente
    # los paneles administrativos, que no aparecen en la navegación pública.
    demo_password: str = ""  # compatibilidad; deliberadamente no usado
    admin_password: str = ""
    admin_parametros: str = ""

    @property
    def meta_api_base(self) -> str:
        return f"https://graph.facebook.com/{self.meta_api_version}"

    @property
    def wc_api_base(self) -> str:
        return f"{self.wc_store_url.rstrip('/')}/wp-json/wc/v3"

    @property
    def whisper_enabled(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def secure_cookies(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
