"""User onboarding intelligence build.

One-time process that analyzes email history to build contact profiles,
topic domains, writing style guide, and per-user scoring model.
"""

from onboarding.runner import run_onboarding

__all__ = ["run_onboarding"]
