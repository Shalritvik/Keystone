"""Runtime configuration and the safety policy.

Two distinct things live here, and the split is deliberate:

* ``Settings`` is *operational* config — endpoints, model names, timeouts.
  It is environment-derived and boring.
* ``Policy`` is the *safety contract* — the allowlist, the permitted action
  types, and the classification of risky actions. It is loaded from a file
  that a human is expected to read and review, and it is versioned alongside
  the artifacts it governs.

Keeping the policy in a reviewable file rather than in environment variables
is intentional: a compliance reviewer at a bank should be able to read one
document and know exactly what the automation is permitted to do.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY_PATH = REPO_ROOT / "policy.yaml"
DEFAULT_ARTIFACT_DIR = REPO_ROOT / "artifacts"
DEFAULT_EVIDENCE_DIR = REPO_ROOT / "evidence"
DEFAULT_ENV_PATH = REPO_ROOT / ".env"


# --------------------------------------------------------------------------
# Operational settings
# --------------------------------------------------------------------------


@dataclass
class Settings:
    """Operational configuration, sourced from the environment."""

    # NVIDIA NIM exposes an OpenAI-compatible surface, so the client is the
    # stock `openai` SDK pointed at a different base URL. That means any other
    # OpenAI-compatible endpoint (vLLM, Ollama, OpenRouter, a local NIM
    # container) is a one-variable swap with no code change.
    llm_api_key: str = ""
    llm_base_url: str = "https://integrate.api.nvidia.com/v1"
    llm_model: str = "openai/gpt-oss-20b"

    # The hosted NIM free tier rate-limits per model and answers with 429.
    # Discovery is the only path that calls the model at all, so we absorb
    # this with backoff rather than trying to engineer around it.
    llm_max_retries: int = 6
    llm_initial_backoff_s: float = 2.0
    llm_max_backoff_s: float = 45.0
    llm_temperature: float = 0.0
    llm_timeout_s: float = 120.0
    # Several NIM-hosted models are reasoning models with a separate,
    # unbounded reasoning trace before any `content` is emitted. Bounding
    # `max_tokens` caps latency/cost for those; it is not a full reliability
    # fix on its own -- one candidate model (moonshotai/kimi-k3) still
    # returned an empty `content` on `finish_reason="stop"` in live testing
    # even with this set, which is why it was rejected as the default in
    # favour of a model that stayed reliable across repeated calls.
    llm_max_tokens: int = 1024

    # Agent loop stopping conditions (3.1).
    max_steps: int = 25
    run_timeout_s: float = 300.0
    max_consecutive_no_progress: int = 3

    # Surface behaviour.
    headless: bool = True
    action_timeout_s: float = 10.0
    settle_timeout_s: float = 8.0

    artifact_dir: Path = DEFAULT_ARTIFACT_DIR
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR
    policy_path: Path = DEFAULT_POLICY_PATH

    @classmethod
    def from_env(cls) -> "Settings":
        # A real environment variable (e.g. set by a deployment) always wins
        # over .env -- override=False -- so .env is purely a local-dev
        # convenience, never a way to shadow production configuration.
        load_dotenv(dotenv_path=DEFAULT_ENV_PATH, override=False)

        def _flag(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        def _num(name: str, default: float) -> float:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        return cls(
            # NVIDIA_API_KEY is the name NIM's own quickstart uses; accept the
            # generic name too so a different provider doesn't need renaming.
            llm_api_key=os.getenv("NVIDIA_API_KEY") or os.getenv("LLM_API_KEY") or "",
            llm_base_url=os.getenv("LLM_BASE_URL", cls.llm_base_url),
            llm_model=os.getenv("LLM_MODEL", cls.llm_model),
            llm_max_tokens=int(_num("LLM_MAX_TOKENS", cls.llm_max_tokens)),
            max_steps=int(_num("GRIP_MAX_STEPS", cls.max_steps)),
            run_timeout_s=_num("GRIP_RUN_TIMEOUT_S", cls.run_timeout_s),
            headless=_flag("GRIP_HEADLESS", cls.headless),
            action_timeout_s=_num("GRIP_ACTION_TIMEOUT_S", cls.action_timeout_s),
            artifact_dir=Path(os.getenv("GRIP_ARTIFACT_DIR", str(DEFAULT_ARTIFACT_DIR))),
            evidence_dir=Path(os.getenv("GRIP_EVIDENCE_DIR", str(DEFAULT_EVIDENCE_DIR))),
            policy_path=Path(os.getenv("GRIP_POLICY", str(DEFAULT_POLICY_PATH))),
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)


# --------------------------------------------------------------------------
# Safety policy
# --------------------------------------------------------------------------

RiskClass = Literal["safe", "risky", "forbidden"]


@dataclass
class Policy:
    """The explicit, configurable guardrail contract (3.4).

    ``allowed_origins`` and ``allowed_route_patterns`` bound *where* the agent
    may act. ``allowed_actions`` bounds *what* it may do. ``risky_actions`` and
    ``risky_control_patterns`` mark the subset that is irreversible or
    consequential and therefore may not run unattended.
    """

    allowed_origins: list[str] = field(default_factory=list)
    allowed_route_patterns: list[str] = field(default_factory=lambda: [".*"])
    denied_route_patterns: list[str] = field(default_factory=list)

    allowed_actions: list[str] = field(
        default_factory=lambda: ["click", "type", "select", "navigate", "read", "wait"]
    )
    risky_actions: list[str] = field(default_factory=list)

    # Control-level risk: a click is normally safe, but a click on a control
    # accessibly named "Post Transaction" is not. Risk is a property of the
    # (action, target) pair, not of the action verb alone.
    risky_control_patterns: list[str] = field(
        default_factory=lambda: [
            r"\b(submit|post|confirm|transfer|withdraw|delete|remove|close\s+account)\b",
        ]
    )
    forbidden_control_patterns: list[str] = field(default_factory=list)

    # How to treat a risky action when nobody is watching. "block" refuses,
    # "escalate" raises an intervention request, "flag" proceeds but records it.
    unattended_risky_disposition: Literal["block", "escalate", "flag"] = "escalate"

    # Redaction: fields whose *values* must never reach an artifact, log, or
    # evidence file. Matched against the control's accessible name.
    sensitive_field_patterns: list[str] = field(
        default_factory=lambda: [
            r"\b(password|passcode|pin|ssn|social\s*security|tax\s*id|secret|token|api[_\s-]?key)\b",
        ]
    )

    version: str = "1"

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Policy":
        path = Path(path) if path else DEFAULT_POLICY_PATH
        if not path.exists():
            raise FileNotFoundError(
                f"Safety policy not found at {path}. The system refuses to run "
                "without an explicit policy; copy policy.example.yaml to policy.yaml."
            )
        raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(raw) - known
        if unknown:
            raise ValueError(
                f"Unknown key(s) in {path}: {sorted(unknown)}. "
                "Refusing to load a policy we don't fully understand."
            )
        return cls(**raw)

    # -- compiled matchers ------------------------------------------------

    def __post_init__(self) -> None:
        self._allowed_routes = [re.compile(p, re.I) for p in self.allowed_route_patterns]
        self._denied_routes = [re.compile(p, re.I) for p in self.denied_route_patterns]
        self._risky_controls = [re.compile(p, re.I) for p in self.risky_control_patterns]
        self._forbidden_controls = [re.compile(p, re.I) for p in self.forbidden_control_patterns]
        self._sensitive_fields = [re.compile(p, re.I) for p in self.sensitive_field_patterns]
        self._allowed_origins = {self._normalize_origin(o) for o in self.allowed_origins}

    @staticmethod
    def _normalize_origin(origin: str) -> str:
        origin = origin.strip().rstrip("/")
        if "://" not in origin:
            origin = "http://" + origin
        parsed = urlparse(origin)
        return f"{parsed.scheme}://{parsed.netloc}".lower()

    # -- queries ----------------------------------------------------------

    def url_allowed(self, url: str) -> tuple[bool, str]:
        """Return (allowed, reason). Default-deny on origin."""
        try:
            parsed = urlparse(url)
        except ValueError:
            return False, f"unparseable url: {url!r}"

        if parsed.scheme not in {"http", "https"}:
            return False, f"scheme {parsed.scheme!r} is not permitted"

        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        if origin not in self._allowed_origins:
            return False, f"origin {origin} is not in the allowlist"

        route = parsed.path or "/"
        for pat in self._denied_routes:
            if pat.search(route):
                return False, f"route {route} matches deny pattern {pat.pattern!r}"

        if not any(pat.search(route) for pat in self._allowed_routes):
            return False, f"route {route} matches no allow pattern"

        return True, "ok"

    def action_allowed(self, action: str) -> tuple[bool, str]:
        if action not in self.allowed_actions:
            return False, f"action type {action!r} is not in allowed_actions"
        return True, "ok"

    def classify(self, action: str, control_name: str | None) -> RiskClass:
        """Classify an (action, target) pair by reversibility."""
        name = (control_name or "").strip()
        if name:
            for pat in self._forbidden_controls:
                if pat.search(name):
                    return "forbidden"
        if action in self.risky_actions:
            return "risky"
        if name:
            for pat in self._risky_controls:
                if pat.search(name):
                    return "risky"
        return "safe"

    def is_sensitive_field(self, control_name: str | None) -> bool:
        name = (control_name or "").strip()
        if not name:
            return False
        return any(pat.search(name) for pat in self._sensitive_fields)
