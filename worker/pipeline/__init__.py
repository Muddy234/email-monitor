"""Pipeline engine — filter, analyze, draft generation."""
from .filter import EmailFilter
from .analyzer import ClaudeAnalyzer
from .drafts import DraftGenerator
from .prompts import get_analysis_prompt, get_draft_prompt_template
