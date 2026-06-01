from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://brianlo@localhost:5432/worldview_dev"
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    gdelt_user_agent: str = "worldview-dev/0.1"

    # LLM for the cluster summarizer. Provider-agnostic: any OpenAI-compatible
    # chat-completions endpoint. Defaults to NVIDIA's hosted DeepSeek.
    llm_api_key: str | None = None
    llm_base_url: str = "https://integrate.api.nvidia.com/v1"
    llm_model: str = "deepseek-ai/deepseek-v4-pro"
    # Max summarizer requests/min. The NVIDIA free NIM tier allows 40 RPM per
    # account, shared across projects; 10 leaves ~30 for everything else.
    llm_max_rpm: int = 10
    # Clusters summarized per ingest cycle. At ~96 cycles/day, 5 ≈ 480 req/day
    # (under free-tier daily caps like Gemini's 1,500/day); 20 ≈ 1,920/day.
    summarizer_batch_size: int = 20
    # Set to false to skip the summarization step entirely.
    # Clustering and dedupe still run; cluster cards just show the
    # representative article's original headline instead of an AP-style
    # synthesized one. Avoids LLM API spend.
    summarizer_enabled: bool = True

    # Cluster tuning
    # 0.85 is stricter than the initial 0.78 — fewer false-positive merges
    # like "5 unrelated US local stories about restaurants and fraud" that the
    # first pass produced. Override via env if you want more or less aggressive.
    cluster_threshold: float = 0.85
    cluster_window_hours: int = 48   # only consider recent clusters as candidates

    # Shared secret for /admin/run-ingest — the CF cron worker presents this
    # in an X-Admin-Token header. Empty string disables the endpoint
    # (defensive default — never enable in prod without setting the secret).
    ingest_token: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
