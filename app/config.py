from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./company_os.db"
    jwt_secret: str = "dev-secret-change-me-in-prod-0123456789abcdef"
    jwt_ttl_seconds: int = 43200  # 12h — long enough that testing sessions don't get logged out
    anthropic_api_key: str | None = None  # optional; gate + tests run without it
    ollama_base_url: str = "http://localhost:11434"  # local models, no key/cost


settings = Settings()
