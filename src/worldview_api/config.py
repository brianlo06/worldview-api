from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://brianlo@localhost:5432/worldview_dev"
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    gdelt_user_agent: str = "worldview-dev/0.1"

    # Anthropic Claude (used by cluster summarizer)
    anthropic_api_key: str | None = None
    claude_summarizer_model: str = "claude-haiku-4-5"
    # Set to false to skip the Claude summarization step entirely.
    # Clustering and dedupe still run; cluster cards just show the
    # representative article's original headline instead of an AP-style
    # synthesized one. Saves the ~$10-15/mo Anthropic spend.
    summarizer_enabled: bool = True

    # Cluster tuning
    # 0.85 is stricter than the initial 0.78 — fewer false-positive merges
    # like "5 unrelated US local stories about restaurants and fraud" that the
    # first pass produced. Override via env if you want more or less aggressive.
    cluster_threshold: float = 0.85
    cluster_window_hours: int = 48   # only consider recent clusters as candidates

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
