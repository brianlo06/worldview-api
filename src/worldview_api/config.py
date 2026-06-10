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

    # --- Interactive "ask the globe" (POST /ask) ------------------------------
    # Budget for interactive LLM synthesis, tracked SEPARATELY from the
    # summarizer so an /ask traffic spike can't starve ingest-time
    # summarization (and vice-versa). When the daily cap or RPM pace is hit,
    # /ask transparently serves the degraded (templated, no-LLM) answer.
    # Sized small on purpose: free-tier, degrade-hard strategy.
    ask_llm_daily_cap: int = 200          # interactive synth calls/day before degrading
    ask_llm_max_rpm: int = 6              # interactive synth pace (req/min)
    ask_llm_timeout_s: float = 6.0       # per-call wall-clock budget before degrading
    # How long a cached /ask answer is considered fresh. Aligned to the ~15-min
    # ingest cycle so answers track the data without re-spending the LLM.
    ask_cache_ttl_seconds: int = 900
    # Coordinate bucketing for "near me" answers: round lat/lon to this many
    # decimals so nearby users share a cache entry (~0.5° ≈ city granularity).
    ask_geo_bucket_decimals: int = 1
    # Retrieval window + breadth for ask cluster search.
    ask_search_hours: int = 48
    ask_search_limit: int = 8
    ask_min_similarity: float = 0.42

    # --- Top-stories briefing (POST /briefing) --------------------------------
    # Budget for the briefing narration LLM call, tracked SEPARATELY from /ask
    # and the summarizer (per the budget-isolation invariant) so a briefing
    # burst can't starve interactive answers or ingest-time summarization.
    # One briefing = one LLM call (the whole script in a single request), and
    # briefings are user-triggered + low-volume, so the cap is modest. When the
    # cap/pace/timeout is hit the endpoint serves a cleaned-up, no-LLM fallback.
    briefing_llm_daily_cap: int = 100     # briefing synth calls/day before degrading
    briefing_llm_max_rpm: int = 6         # briefing synth pace (req/min)
    briefing_llm_timeout_s: float = 8.0   # per-call wall-clock budget (5-story call is heavier than /ask)
    # How many top stories a briefing covers.
    briefing_story_count: int = 5
    # Model for briefing narration. Provider free tiers cap requests PER MODEL
    # per day, so pointing the briefing at a DIFFERENT model than the summarizer
    # (LLM_MODEL) gives it its own daily bucket — the summarizer's churn can't
    # starve the rare, user-triggered briefing. Empty = inherit LLM_MODEL.
    briefing_llm_model: str = ""

    # --- Share cards (/share, /s/<id>) ----------------------------------------
    # Where rendered 1200x630 PNG cards are cached on disk (immutable per id).
    share_card_dir: str = "/tmp/worldview-share-cards"
    # Public base URL the share HTML redirects humans into (the SPA apex).
    share_redirect_base: str = "https://jarvisworlds.com"
    # Absolute base for og:image / share links (apex; Caddy routes /s/* to API).
    share_public_base: str = "https://jarvisworlds.com"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
