from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "mysql+pymysql://openami:local@localhost/open_grid_loss"
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_auto_offset_reset: str = "earliest"
    rabbitmq_host: str = "localhost"
    rabbitmq_user: str = "openami"
    rabbitmq_password: str = "local"
    api_key: str = Field(min_length=32)
    read_api_key: str = Field(min_length=16)
    public_reads: bool = False
    service: str = "api"
    max_bulk_readings: int = Field(default=1000, ge=1, le=10000)
    baseline_days: int = Field(default=28, ge=1)
    baseline_min_samples: int = Field(default=14, ge=1)
    allowed_lateness_minutes: int = Field(default=30, ge=0)
    min_completeness: float = Field(default=98, ge=0, le=100)
    deviation_pp: float = Field(default=5, gt=0)
    min_impact_kwh: float = Field(default=0.1, ge=0)
    persistence_required: int = Field(default=4, ge=1, le=6)
    max_retries: int = Field(default=5, ge=1)
    log_level: str = "INFO"
    ui_admin_username: str = ""
    ui_admin_password: str = ""
    ui_secure_cookies: bool = False
    ui_demo_enabled: bool = True
    grafana_url: str = "http://localhost:3000"


@lru_cache
def settings() -> Settings:
    return Settings()
