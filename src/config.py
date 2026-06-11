from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    DATABASE_URL: str
    SUPER_ADMIN_ID: str
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    BASE_URL: str = ""  # e.g. https://your-domain.com — used for webhook setup

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
