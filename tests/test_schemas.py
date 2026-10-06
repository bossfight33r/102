import pytest
from pydantic import ValidationError

from radar.llm.fake import DEFAULT_ANALYSIS
from radar.schemas import AnalysisDraft, Button, ChannelStatus, Niche


def test_niche_defaults_and_validation():
    n = Niche(id="ai-tools", name="AI")
    assert n.format == "both" and n.enabled
    with pytest.raises(ValidationError):
        Niche(id="Bad Id", name="x")
    with pytest.raises(ValidationError):
        Niche(id="x", name="x", min_subs=10, max_subs=5)
    with pytest.raises(ValidationError):
        Niche(id="x", name="x", unknown_field=1)


def test_analysis_draft_strict():
    AnalysisDraft.model_validate(DEFAULT_ANALYSIS)
    bad = dict(DEFAULT_ANALYSIS, extra="nope")
    with pytest.raises(ValidationError):
        AnalysisDraft.model_validate(bad)
    with pytest.raises(ValidationError):
        AnalysisDraft.model_validate(dict(DEFAULT_ANALYSIS, confidence=1.5))
    missing = {k: v for k, v in DEFAULT_ANALYSIS.items() if k != "idea_for_my_channel"}
    with pytest.raises(ValidationError):
        AnalysisDraft.model_validate(missing)


def test_button_callback_limit():
    Button(text="x", callback_data="fb:t:" + "a" * 11)
    with pytest.raises(ValidationError):
        Button(text="x", callback_data="x" * 65)


def test_enum_values():
    assert ChannelStatus("candidate") is ChannelStatus.CANDIDATE
