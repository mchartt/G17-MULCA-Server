"""
The course material and project schedule given to the main agent only:
config/knowledge, loader.load_knowledge, course_schedule.
"""

import re
from datetime import date

import pytest

from muca_server.agents.openai.factory import OpenAIAgentFactory
from muca_server.agents.openai.model_settings import ModelSettings
from muca_server.agents.options import AuxiliaryAgentOptions, MainAgentOptions
from muca_server.cognition.course_schedule import next_deliverables, render_schedule
from muca_server.cognition.loader import (
    DEFAULT_CONFIG_DIR,
    load_course_schedule,
    load_knowledge,
    load_strategy_store,
)

SCHEDULE = load_course_schedule()

REQUIREMENTS = "initial user requirements analysis"
UX = "initial UX design (with scenarios)"
REFINED = "refined UX design"
PROTOTYPING = "initial prototyping, at the following tutoring"


@pytest.mark.parametrize(
    ("today", "group_a", "group_b"),
    [
        (date(2026, 10, 7), f"{REQUIREMENTS}", f"{REQUIREMENTS}"),
        (date(2026, 10, 12), "Mon 12 Oct 2026", "Wed 14 Oct 2026"),  # due today
        (date(2026, 10, 13), f"{UX}, at the tutoring on Mon 26 Oct", REQUIREMENTS),
        (date(2026, 10, 27), REFINED, f"{UX}, at the tutoring on Wed 28 Oct"),
        (date(2026, 11, 3), PROTOTYPING, f"{REFINED}"),
        (date(2026, 11, 10), PROTOTYPING, PROTOTYPING),
    ],
)
def test_each_group_is_told_its_own_next_deliverable(today, group_a, group_b):
    a, b = next_deliverables(SCHEDULE, today).split("; Group B: ")

    assert group_a in a
    assert group_b in b


def test_the_schedule_stops_at_the_listed_deliverables():
    text = render_schedule(SCHEDULE)

    for item in SCHEDULE["deliverables"]:
        assert item["what"] in text
    assert "Wed 4 Nov 2026" in text
    assert "Dec" not in text
    assert "not tracked here" in text


def test_the_knowledge_holds_every_file_with_its_type_version_and_source():
    text = load_knowledge()
    files = sorted((DEFAULT_CONFIG_DIR / "knowledge").glob("*.md"))

    assert files
    # Each file (and the schedule) opens with its title, then a line giving
    # its type, version and source -- what a teacher updates with it.
    headers = [path.read_text(encoding="utf-8").splitlines()[:2] for path in files]
    headers.append(render_schedule(SCHEDULE).splitlines()[:2])
    for title, header in headers:
        assert title.startswith("# ") and title in text
        assert re.match(r"Type: (normative|support) · Version: v\d+ \(.+\) · Source: .+", header)
        assert header in text


def test_only_the_main_agent_gets_the_course_material(tmp_path):
    factory = OpenAIAgentFactory(
        client=object(),
        strategies=load_strategy_store(["respond_when_addressed"]),
        main_persona="Main persona.",
        intervention_persona="Observer persona.",
        task_knowledge="The task.",
        models=ModelSettings(tmp_path / "models.json"),
        main_knowledge="COURSE MATERIAL",
    )

    main = factory.build_main(MainAgentOptions())._build_instructions()
    observer = factory.build_auxiliary(
        AuxiliaryAgentOptions.for_type("intervention_detection"),
    )._build_instructions()

    assert "The task." in main and "COURSE MATERIAL" in main
    assert "The task." in observer and "COURSE MATERIAL" not in observer
