"""Test that canon guidance is injected into the kanban lifecycle prompt.

Canon rule: workers must cite `CANON: read <file>#<heading>` in their first
comment before building, and pass `metadata={'canon_learned': [...]}` on
completion. This test verifies the guidance is present in the dispatched
worker's system prompt so they learn the rule BEFORE the gate refuses them.

Reference: t_68966469 (2026-09-24)
"""

import pytest
from agent.prompt_builder import KANBAN_GUIDANCE


def test_canon_guidance_present_in_kanban_lifecycle():
    """The KANBAN_GUIDANCE block must teach the canon rule upfront."""
    # The guidance should name the rule
    assert "Canon" in KANBAN_GUIDANCE, "Canon rule not mentioned in KANBAN_GUIDANCE"
    assert "read before build" in KANBAN_GUIDANCE, "Canon rule not teaching read-before-build"
    assert "write back on finish" in KANBAN_GUIDANCE, "Canon rule not teaching write-back"
    
    # The guidance should explain the format
    assert "CANON: read canon/" in KANBAN_GUIDANCE, "Canon format not documented"
    assert "canon_learned" in KANBAN_GUIDANCE, "canon_learned metadata not documented"
    
    # The guidance should warn about refusal
    assert "Completion is refused" in KANBAN_GUIDANCE, "Completion refusal not warned"
    

def test_canon_guidance_names_affected_areas():
    """Canon guidance must name which areas it covers."""
    assert "Sage" in KANBAN_GUIDANCE, "Sage not named as canon area"
    assert "Gmail" in KANBAN_GUIDANCE, "Gmail not named as canon area"
    assert "dash" in KANBAN_GUIDANCE, "Dashboard not named as canon area"
    assert "feedback" in KANBAN_GUIDANCE, "Feedback not named as canon area"


def test_canon_guidance_exact_format():
    """Canon guidance must show the exact format for comments and metadata."""
    # First comment format
    assert "CANON: read canon/<file>#<heading>" in KANBAN_GUIDANCE, \
        "First comment format not shown exactly"
    
    # Metadata format
    assert "metadata={'canon_learned':" in KANBAN_GUIDANCE, \
        "Metadata format not shown exactly"
    
    # Note that 'nothing new' is allowed
    assert "'nothing new'" in KANBAN_GUIDANCE, \
        "'nothing new' exception not documented"


def test_canon_guidance_within_lifecycle_section():
    """Canon rule should be in the ## Lifecycle section, not elsewhere."""
    # Find the Lifecycle section
    lifecycle_start = KANBAN_GUIDANCE.find("## Lifecycle")
    assert lifecycle_start != -1, "## Lifecycle section not found"
    
    # Find Orchestrator mode (next major section)
    orchestrator_start = KANBAN_GUIDANCE.find("## Orchestrator mode")
    assert orchestrator_start != -1, "## Orchestrator mode section not found"
    
    # Canon guidance should be between these
    canon_pos = KANBAN_GUIDANCE.find("Canon: read before build")
    assert lifecycle_start < canon_pos < orchestrator_start, \
        "Canon guidance not in Lifecycle section where workers expect it"


def test_canon_guidance_before_step_5():
    """Canon guidance should appear between step 4 (block) and step 5/6 (finish)."""
    step4_pos = KANBAN_GUIDANCE.find("4. **Block on genuine ambiguity.**")
    assert step4_pos != -1, "Step 4 not found"
    
    # After our change, step 5 becomes step 6
    step5_or_6_pos = KANBAN_GUIDANCE.find("**Finish with the review model")
    assert step5_or_6_pos != -1, "Finish step not found"
    
    canon_pos = KANBAN_GUIDANCE.find("Canon: read before build")
    assert step4_pos < canon_pos < step5_or_6_pos, \
        "Canon guidance not positioned between step 4 and finish"
