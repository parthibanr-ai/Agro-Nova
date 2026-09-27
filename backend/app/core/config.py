from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./agrin.db"
    cors_origins: str = "http://localhost:8080,http://localhost:5173"
    default_country: str = "IN"

    auth_mode: str = "dev"  # "dev" | "firebase"
    firebase_credentials_path: str | None = None
    admin_api_key: str = "change-me"

    ee_auth_mode: str = "user"  # "service_account" | "user"
    gee_service_account_email: str | None = None
    gee_service_account_key_path: str | None = None
    gee_cloud_project: str | None = None

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_use_vertex: bool = False
    gcp_location: str = "asia-south1"

    translate_api_key: str | None = None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
