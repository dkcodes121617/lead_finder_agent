"""Configuration for the Lead Finder.

Everything comes from this folder's `.env`, or from the real process environment
in production (Modal injects it there). Nothing outside this folder is read —
that is what makes the folder deployable on its own.

The validation rule that matters: **assert only what `SOURCES_ENABLED` actually
needs.** A run that reaches node nine before discovering a missing Places key has
wasted eight nodes of budget, and a run that refuses to start because a *disabled*
source has no key is worse still — it makes turning a source off useless.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from wizcore.config import ConfigError, env_bool, env_int, env_list, env_str, load_env

AGENT_ROOT = Path(__file__).resolve().parent
load_env(AGENT_ROOT)

AGENT_NAME = "lead_finder"

# Which credentials each source cannot run without. Sources absent from this map
# need nothing (Hacker News has no key; OSM's Overpass endpoint is public).
SOURCE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "inbound": ("NEON_DATABASE_URL",),
    "hackernews": (),
    "osm": (),
    "rss": (),
    "reddit": ("REDDITAPIS_KEY", "REDDITAPIS_BASE_URL"),
    "twitter": ("TWITTERAPIS_KEY", "TWITTERAPIS_BASE_URL"),
    "stackexchange": ("STACKEXCHANGE_KEY",),
    "places": ("GOOGLE_PLACES_API_KEY",),
}

ALL_SOURCES = tuple(SOURCE_REQUIREMENTS)


@dataclass(frozen=True)
class Config:
    # ── identity / behaviour ──
    dry_run: bool = env_bool("DRY_RUN", True)
    log_level: str = env_str("LOG_LEVEL", "INFO")
    display_tz: str = env_str("DISPLAY_TZ", "Asia/Kolkata")

    # ── the message bus ──
    database_url: str = env_str("NEON_DATABASE_URL")
    checkpoint_schema: str = env_str("LANGGRAPH_CHECKPOINT_SCHEMA", "lf_ckpt")

    # ── site facts (read-only) ──
    site_repo: str = env_str("SITE_REPO", "dkcodes121617/wizcodes_main_website")
    site_branch: str = env_str("SITE_REPO_BRANCH", "main")
    site_read_token: str = env_str("SITE_READ_TOKEN")
    # Set locally to iterate against the sibling checkout without spending API
    # calls. Unset in production, where there is no checkout.
    site_local_dir: str = env_str("SITE_LOCAL_DIR")

    # ── sources ──
    sources_enabled: list[str] = field(default_factory=lambda: env_list("SOURCES_ENABLED", "inbound,hackernews"))
    max_candidates_per_run: int = env_int("MAX_CANDIDATES_PER_RUN", 300)
    lookback_minutes: int = env_int("LOOKBACK_MINUTES", 45)

    # reddit
    redditapis_key: str = env_str("REDDITAPIS_KEY")
    redditapis_base_url: str = env_str("REDDITAPIS_BASE_URL")
    # Stored, not hardcoded: every obvious guess at this path 404s, and the next
    # reader should not have to rediscover that. Verified live.
    redditapis_posts_path: str = env_str("REDDITAPIS_POSTS_PATH", "/api/reddit/posts")
    reddit_backend: str = env_str("REDDIT_BACKEND", "redditapis")
    reddit_user_agent: str = env_str("REDDIT_USER_AGENT", "web:site.wizcodes.leadfinder:v1.0")
    subreddits: list[str] = field(default_factory=lambda: env_list("SUBREDDITS", "smallbusiness"))

    # twitter
    twitterapis_key: str = env_str("TWITTERAPIS_KEY")
    twitterapis_base_url: str = env_str("TWITTERAPIS_BASE_URL")
    twitter_queries: list[str] = field(default_factory=lambda: env_list("TWITTER_QUERIES"))

    # hacker news
    hn_base_url: str = env_str("HN_BASE_URL", "https://hn.algolia.com/api/v1")
    hn_queries: list[str] = field(default_factory=lambda: env_list("HN_QUERIES", "looking for developer"))

    # stack exchange
    stackexchange_key: str = env_str("STACKEXCHANGE_KEY")
    stackexchange_sites: list[str] = field(default_factory=lambda: env_list("STACKEXCHANGE_SITES", "stackoverflow"))
    stackexchange_tags: list[str] = field(default_factory=lambda: env_list("STACKEXCHANGE_TAGS"))

    # places / osm
    google_places_api_key: str = env_str("GOOGLE_PLACES_API_KEY")
    discovery_queries: list[str] = field(default_factory=lambda: env_list("DISCOVERY_QUERIES"))
    # §7.2: websiteUri is an Enterprise-tier field and a request bills at the
    # highest tier it touches, so the free ceiling is ~1,000/month, not 10,000.
    # Since the website URL is the entire point of the weak-site pipeline, every
    # discovery call is an Enterprise call. Cap it in code, not in hope.
    places_max_calls_per_run: int = env_int("PLACES_MAX_CALLS_PER_RUN", 60)
    overpass_url: str = env_str("OVERPASS_URL", "https://overpass-api.de/api/interpreter")

    # rss
    rss_feeds: list[str] = field(default_factory=lambda: env_list("RSS_FEEDS"))

    # ── classification ──
    classifier_provider: str = env_str("CLASSIFIER_PROVIDER", "groq")
    groq_api_key: str = env_str("GROQ_API_KEY")
    groq_classify_model: str = env_str("GROQ_CLASSIFY_MODEL", "llama-3.3-70b-versatile")
    # Measured, not guessed: at 300 candidates a run only the fast model is
    # viable — haiku answered in 4.2s where opus-5 took 10.6s, and all models
    # agreed on the labels.
    fallback_classify_model: str = env_str("ANTHROPIC_FALLBACK_CLASSIFY_MODEL", "claude-haiku-4.5")
    # The reply-angle draft is the one place wording matters here, so it gets the
    # model that invented no figures in the bench.
    voice_model: str = env_str("ANTHROPIC_VOICE_MODEL", "claude-opus-4-8")
    classify_batch_size: int = env_int("CLASSIFY_BATCH_SIZE", 12)
    reply_angle_min_score: int = env_int("REPLY_ANGLE_MIN_SCORE", 70)

    # ── notification ──
    notify_min_score: int = env_int("NOTIFY_MIN_SCORE", 55)
    notify_max_per_run: int = env_int("NOTIFY_MAX_PER_RUN", 8)
    inbound_min_score: int = env_int("INBOUND_MIN_SCORE", 0)

    # ── budget caps, per day, per provider ──
    # A runaway loop costs one day's cap instead of one month's budget.
    budget_caps: dict[str, float] = field(
        default_factory=lambda: {
            "redditapis": env_int("BUDGET_REDDITAPIS_READS", 400),
            "twitterapis": env_int("BUDGET_TWITTERAPIS_READS", 200),
            "places": env_int("BUDGET_PLACES_CALLS", 60),
            "groq": env_int("BUDGET_GROQ_CALLS", 400),
            "claude_proxy": env_int("BUDGET_CLAUDE_KTOKENS", 400),
        }
    )

    # ── source health ──
    # A source that has failed this many times running is skipped and alerted
    # once, instead of alerting every 30 minutes forever.
    fail_streak_skip: int = env_int("FAIL_STREAK_SKIP", 5)
    http_timeout: int = env_int("HTTP_TIMEOUT", 25)

    def active_sources(self) -> list[str]:
        """Enabled sources, filtered to ones this system knows about."""
        return [s for s in self.sources_enabled if s in SOURCE_REQUIREMENTS]

    def unknown_sources(self) -> list[str]:
        return [s for s in self.sources_enabled if s not in SOURCE_REQUIREMENTS]

    def validate(self) -> None:
        """Fail fast, before anything is spent, listing every problem at once."""
        problems: list[str] = []
        if not self.database_url:
            problems.append("NEON_DATABASE_URL is not set (it is the message bus)")
        if unknown := self.unknown_sources():
            problems.append(
                f"SOURCES_ENABLED names unknown source(s): {', '.join(unknown)}. "
                f"Known: {', '.join(ALL_SOURCES)}"
            )
        if not self.active_sources():
            problems.append("SOURCES_ENABLED is empty - the run would do nothing")

        for source in self.active_sources():
            for key in SOURCE_REQUIREMENTS[source]:
                if not env_str(key):
                    problems.append(f"source '{source}' needs {key}, which is not set")

        if self.classifier_provider == "groq" and not self.groq_api_key:
            problems.append("CLASSIFIER_PROVIDER=groq but GROQ_API_KEY is not set")
        if not self.site_read_token and not self.site_local_dir:
            problems.append(
                "SITE_READ_TOKEN is not set and SITE_LOCAL_DIR is empty - grounding "
                "facts cannot be built"
            )
        if problems:
            raise ConfigError(
                "lead_finder configuration is not runnable:\n  - " + "\n  - ".join(problems)
            )


CONFIG = Config()
