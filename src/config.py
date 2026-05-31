from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    DATABASE_URL: str
    TELEGRAM_BOT_TOKEN: str
    SUPER_ADMIN_ID: str

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()