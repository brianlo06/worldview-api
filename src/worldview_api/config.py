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

    # --- Anomaly synopsis (background, generated at detection time) ----------
    # One short LLM line per new/changed anomaly explaining the spike. Its own
    # budget per the budget-isolation invariant; degrades to a template line.
    anomaly_llm_daily_cap: int = 60
    anomaly_llm_max_rpm: int = 6
    anomaly_llm_timeout_s: float = 6.0
    # Empty = inherit BRIEFING_LLM_MODEL (then LLM_MODEL) — the briefing's
    # model is the one provisioned for short interactive rewrites.
    anomaly_llm_model: str = ""
    # Model for briefing narration. Provider free tiers cap requests PER MODEL
    # per day, so pointing the briefing at a DIFFERENT model than the summarizer
    # (LLM_MODEL) gives it its own daily bucket — the summarizer's churn can't
    # starve the rare, user-triggered briefing. Empty = inherit LLM_MODEL.
    briefing_llm_model: str = ""

    # --- Briefing holograms (AI scene renders, GET /holo/<id>) ----------------
    # One stylized "holographic reconstruction" image per briefing story,
    # rendered in the background by the Gemini image model and served from
    # /holo/<cluster_id> for the frontend's rotating-hologram projection.
    # Best-effort with its own budget (isolation invariant): on cap, pace,
    # timeout, or refusal the client simply keeps the article photo.
    holo_enabled: bool = True
    # Render provider. "pollinations": free Flux endpoint, no key needed
    # (anonymous tier — our 4-rpm pacing stays inside its limits).
    # "gemini": the Gemini image model — better quality but needs a
    # billing-enabled key; the free tier has ZERO image-gen quota.
    holo_provider: str = "pollinations"
    # The current Pollinations platform (gen.pollinations.ai). The legacy
    # tokenless host (image.pollinations.ai) is saturated/sunset — it 402s
    # immediately and ignores tokens.
    holo_pollinations_base: str = "https://gen.pollinations.ai"
    holo_pollinations_model: str = "flux"
    # API token (free: register at https://enter.pollinations.ai), sent as a
    # Bearer header. Without it renders just don't happen and the briefing
    # degrades to article-photo holograms as usual.
    holo_pollinations_token: str = ""
    # Gemini settings (holo_provider="gemini"). Native endpoint — the
    # OpenAI-compat layer the text LLMs use doesn't expose image output.
    # Key defaults to LLM_API_KEY (already a Gemini key in prod).
    holo_model: str = "gemini-2.5-flash-image"
    holo_api_base: str = "https://generativelanguage.googleapis.com/v1beta"
    holo_api_key: str = ""
    # Renders are cached per cluster and briefings replay for 20 min, so the
    # cap is rarely approached; it bounds spend if the provider is paid.
    holo_daily_cap: int = 60
    holo_max_rpm: int = 4
    # Image generation is slow (Pollinations can take 30s+ under load); the
    # render thread is off the request path so a generous timeout is free.
    holo_timeout_s: float = 60.0
    holo_dir: str = "/tmp/worldview-holograms"
    holo_max_age_hours: int = 72

    # --- Neural TTS (GET /tts) -------------------------------------------------
    # Piper voice for JARVIS lines (briefing narration, greeting). The model
    # is baked into the image at /app/voices (docker/Dockerfile); local dev
    # can point TTS_VOICE elsewhere or leave it missing — synthesis simply
    # degrades and the client falls back to browser speech.
    tts_enabled: bool = True
    tts_voice: str = "/app/voices/en_GB-alan-medium.onnx"
    tts_dir: str = "/tmp/worldview-tts"
    tts_max_chars: int = 700
    # ~7 segments per briefing + static UI lines; unique text is cached, so
    # this cap is generous headroom, not expected spend.
    tts_daily_cap: int = 1500
    tts_timeout_s: float = 30.0
    # Piper pacing (>1 = slower). Alan at 1.05 sits close to JARVIS cadence.
    tts_length_scale: float = 1.05
    tts_max_age_hours: int = 72

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
