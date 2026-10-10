from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.config import Settings
from app.errors import AppError
from app.schemas import SourceProfileExtract, SourceSkill, SourceSpan
from app.services.profiles import extract_profile, project_source_profile, read_pdf


def source_profile(**changes):
    value = dict(
        name="Alex Rivera",
        name_source=SourceSpan(start=1,end=1),
        skills=[SourceSkill(name="C++", source=SourceSpan(start=2, end=3))],
        projects=[],
        experience=[SourceSpan(start=4, end=6)],
        education=[],
        languages=[],
        summary=[],
        activities=[],
        achievements=[],
    )
    return SourceProfileExtract(**(value | changes))


SOURCE = "Alex Rivera\nProgramming: Python, C and\nC++\nMaintenance Engineer 2022 - 2024\nAcme Works\nRepaired conveyor sensors.\nUnmapped substantive fact."


def test_identity_uses_literal_selected_source_instead_of_rewritten_model_spelling():
    result=project_source_profile(SOURCE,source_profile(name='Alex Rivers'))
    assert result.name=='Alex Rivera'
    assert any('rewritten model spelling' in note for note in result.extraction_notes)


def test_named_office_tools_accept_explicit_short_names_without_assuming_all_office_tools():
    from app.services.profiles import source_mentions
    assert source_mentions('Microsoft Word and Excel','Microsoft Excel')
    assert source_mentions('Some experience of Access','Microsoft Access')
    assert not source_mentions('Microsoft Office','Microsoft Excel')


def test_date_only_right_gutter_stays_attached_to_the_correct_employer_row():
    from app.services.pdf_layout import column_text
    words=[]
    for y,values in [(10,[(40,'Claims'),(90,'Assistant'),(550,'Summer'),(610,'2020')]),(80,[(40,'Sales'),(90,'Assistant'),(550,'2021'),(600,'-'),(620,'2022')])]:
        for x,value in values:
            words.append({'top':y,'x0':x,'x1':x+len(value)*5,'text':value})
    assert column_text(words).splitlines()==['Claims Assistant Summer 2020','Sales Assistant 2021 - 2022']


def test_award_misclassified_as_a_skill_is_retained_in_achievements():
    text='Alex Rivera\nEDUCATION\nWinner of Independent Research Prize 2024'
    result=project_source_profile(text,source_profile(skills=[SourceSkill(name='Independent Research Prize',source=SourceSpan(start=3,end=3))],experience=[]))
    assert result.skills==[]
    assert result.achievements==['Winner of Independent Research Prize 2024']


def test_overlapping_project_labels_and_omitted_explicit_awards_are_completed_transparently():
    text='Alex Rivera\nPROJECTS\nPump Controller | 2020\nControl Course\nEXPERTISE\nSimulation: Used tools (e.g., Modelica) for testing.\nAWARDS\nEngineering Prize | 2021\nIndependent Institute'
    result=project_source_profile(text,source_profile(skills=[],experience=[],projects=[SourceSpan(start=3,end=3),SourceSpan(start=4,end=4)]))
    assert result.projects==['Pump Controller | 2020\nControl Course']
    assert result.achievements==['Engineering Prize | 2021\nIndependent Institute']
    assert result.skills[0].name=='Modelica'
    assert result.skills[0].evidence in text
    assert any('retained locally' in note for note in result.extraction_notes)


def test_explicit_wrapped_skill_list_is_preserved_when_model_selects_only_category():
    text='Alex Rivera\nEXPERTISE\nProgramming: Julia, R and\nRust\nAWARDS\nResearch Prize | 2022'
    result=project_source_profile(text,source_profile(skills=[SourceSkill(name='Programming',source=SourceSpan(start=3,end=4))],experience=[]))
    assert {'Julia','R','Rust'} <= {skill.name for skill in result.skills}
    assert all(skill.level=='declared' and skill.evidence in text for skill in result.skills)
    assert any('retained locally' in note for note in result.extraction_notes)


def test_model_combined_adjacent_projects_are_separated_without_losing_literal_evidence():
    text = "Alex Rivera\nProjects\nPump Controller | 2021\nControl Course\nVibration Monitor | 2022\nMeasurement Course"
    result = project_source_profile(text,source_profile(skills=[],experience=[],projects=[SourceSpan(start=3,end=6)]))
    assert result.projects == ['Pump Controller | 2021\nControl Course', 'Vibration Monitor | 2022\nMeasurement Course']
    assert all(item in text for item in result.projects)
    assert any('Adjacent projects separated' in note for note in result.extraction_notes)


def test_partial_project_range_is_completed_without_a_duplicate_fragment():
    text = "Alex Rivera\nProjects\nIndustrial Pump\nController |\n2022\nControl Course"
    result = project_source_profile(text,source_profile(skills=[],experience=[],projects=[SourceSpan(start=3,end=4)]))
    assert result.projects == ['Industrial Pump\nController |\n2022\nControl Course']


def test_project_span_ending_in_next_title_does_not_duplicate_that_title():
    text = "Alex Rivera\nProjects\nPump Controller | 2021\nControl Course\nVibration Monitor\n| 2022\nMeasurement Course"
    result = project_source_profile(text,source_profile(skills=[],experience=[],projects=[SourceSpan(start=3,end=5),SourceSpan(start=5,end=7)]))
    assert result.projects == ['Pump Controller | 2021\nControl Course', 'Vibration Monitor\n| 2022\nMeasurement Course']


def test_omitted_labeled_skill_keeps_full_prose_and_declared_status():
    text = "Alex Rivera\nExpertise\nResearch and Innovation: Investigating unusual sensor response patterns\nthrough documented experiments across several independent prototype systems."
    result = project_source_profile(text,source_profile(skills=[],experience=[]))
    skill = next(item for item in result.skills if item.name == 'Research and Innovation')
    assert skill.level == 'declared'
    assert skill.evidence == text.split('Expertise\n',1)[1]


def test_model_omitted_projects_are_kept_with_wrapped_titles_and_course_boundaries():
    text='Alex Rivera\nPROJECTS\nParking Sensor | 2020\nPhysics Course\nIndustrial Pump\nController |\n2022\nControl Course\nEDUCATION\nIndependent Institute 2023'
    result=project_source_profile(text,source_profile(skills=[],experience=[]))
    assert result.projects==['Parking Sensor | 2020\nPhysics Course','Industrial Pump\nController |\n2022\nControl Course']
    assert any('source projects' in note for note in result.extraction_notes)


def test_source_ranges_keep_literal_wrapped_text_complete_duties_and_unmapped_content():
    result = project_source_profile(SOURCE, source_profile())
    assert result.skills[0].evidence == "Programming: Python, C and\nC++"
    assert result.skills[0].level == "declared"
    assert result.experience == ["Maintenance Engineer 2022 - 2024\nAcme Works\nRepaired conveyor sensors."]
    assert result.source_text == SOURCE
    assert result.unmapped_text == ["Unmapped substantive fact."]
    assert result.target_roles == result.locations == []


@pytest.mark.parametrize("span", [SourceSpan(start=9, end=10), SourceSpan(start=3, end=2)])
def test_invalid_source_ranges_fail_closed(span):
    with pytest.raises(AppError, match="outside this CV"):
        project_source_profile(SOURCE, source_profile(experience=[span]))


def test_unsupported_skill_is_not_promoted_from_an_unrelated_source_range():
    with pytest.raises(AppError, match="did not match"):
        project_source_profile(
            SOURCE, source_profile(skills=[SourceSkill(name="Rust", source=SourceSpan(start=2, end=3))])
        )


def test_off_by_one_skill_range_is_repaired_only_to_nearby_literal_evidence():
    result = project_source_profile(SOURCE,source_profile(skills=[SourceSkill(name="C++",source=SourceSpan(start=2,end=2))]))
    assert "C++" in result.skills[0].evidence
    assert result.skills[0].evidence in SOURCE


def test_c_is_not_supported_by_cplusplus_alone_and_adjacent_course_is_preserved():
    from app.services.skills import mentions
    assert not list(mentions("C++ and C#", "C"))
    assert list(mentions("C and C++", "C"))
    source="Alex Rivera\nSkills: Python\nProjects\nPump Controller | 2023\nControl Course"
    value=source_profile(skills=[SourceSkill(name="Python",source=SourceSpan(start=2,end=2))],experience=[],projects=[SourceSpan(start=4,end=4)])
    result=project_source_profile(source,value)
    assert result.projects==["Pump Controller | 2023\nControl Course"]


@pytest.mark.asyncio
async def test_real_model_contract_uses_ranges_and_consent(monkeypatch):
    call = AsyncMock(return_value=source_profile())
    monkeypatch.setattr("app.integrations.llm.LLMClient.structured", call)
    settings = Settings(_env_file=None, mode="live", llm_model="synthetic-test", llm_api_key="synthetic")
    result = await extract_profile(SOURCE, settings, True)
    assert call.call_args.args[0] is SourceProfileExtract
    assert "4: Maintenance Engineer" in call.call_args.args[2]
    assert result.extraction_method == "model_source_ranges"
    with pytest.raises(AppError, match="Confirm external-model"):
        await extract_profile(SOURCE, settings, False)
    assert call.await_count == 1


def test_pdf_visual_order_overrides_reversed_stream_order_and_keeps_both_columns():
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 1 0 0 1 40 600 Tm (Maintained production sensors.) Tj ET\nBT /F1 12 Tf 1 0 0 1 40 680 Tm (Maintenance Engineer 2022) Tj ET\nBT /F1 12 Tf 1 0 0 1 40 760 Tm (Alex Rivera) Tj ET\n0 500 m 600 500 l S\nBT /F1 12 Tf 1 0 0 1 40 440 Tm (Parking Sensor Project 2020) Tj ET\nBT /F1 12 Tf 1 0 0 1 40 420 Tm (Physics course) Tj ET\nBT /F1 12 Tf 1 0 0 1 350 440 Tm (Pump Controller Project 2021) Tj ET\nBT /F1 12 Tf 1 0 0 1 350 420 Tm (Control course) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    text = read_pdf(output.getvalue())
    assert (
        text.index("Alex Rivera") < text.index("Maintenance Engineer") < text.index("Maintained production")
    )
    assert (
        text.index("Parking Sensor")
        < text.index("Physics course")
        < text.index("Pump Controller")
        < text.index("Control course")
    )
