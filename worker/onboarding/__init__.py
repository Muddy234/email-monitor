"""User onboarding intelligence build.

One-time process that analyzes 30 days of email to build contact profiles,
topic domains, writing style guide, and calibration data.
"""

from onboarding.runner import run_onboarding
from onboarding.calibration import run_calibration

__all__ = ["run_onboarding", "run_calibration"]
