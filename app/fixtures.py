"""Synthetic jobs/mail only. Omar example facts come from the supplied CV."""

from datetime import UTC, datetime, timedelta


def example_profile():
    skills = [
        "Python",
        "Java",
        "C++",
        "JavaScript",
        "HTML",
        "CSS",
        "RAG",
        "Pydantic",
        "PyTorch",
        "scikit-learn",
        "TensorFlow",
        "Hugging Face",
        "LangGraph",
        "CrewAI",
        "MCP",
        "OpenAI Agents SDK",
        "SQLite",
        "FAISS",
        "Chroma",
        "Qdrant",
        "AWS",
        "Docker",
        "Gradio",
        "Tkinter",
        "asyncio",
        "Git",
        "OCR",
    ]
    profile = {
        "name": "Omar Aboalola",
        "skills": [
            {"name": skill, "level": "declared", "evidence": f"Supplied CV, Core Skills: {skill}"}
            for skill in skills
        ],
        "projects": ["Monsif — Arabic Legal AI Assistant", "Deep Research Agent", "Production RAG Backend"],
        "experience": ["AI / Data Science Intern — Corelia, Feb–Jul 2025"],
        "education": ["B.Sc. Computer Science, Minor Data Science, CIC 2025"],
        "languages": ["Arabic (Native)", "English (C1)", "German (B1)"],
        "target_roles": ["AI Engineer", "LLM Engineer", "ML Engineer"],
        "locations": ["Egypt"],
        "remote": True,
        "seniority": "entry",
        "employment_type": "Any",
        "salary_minimum": None,
        "salary_currency": "",
        "industries": [],
        "willing_to_learn": True,
        "priorities": "Suggested demo goals; confirm or edit. Interviews and direct recruiter requests first.",
    }
    profile["skills"].append(
        {"name": "FastAPI", "level": "demonstrated", "evidence": "GitHub mini-rag README: FastAPI API layer."}
    )
    return profile


def jobs():
    now = datetime.now(UTC).isoformat()
    titles = [
        "Junior AI Engineer",
        "LLM Engineer",
        "Junior ML Engineer",
        "Applied AI Engineer",
        "RAG Engineer",
        "Machine Learning Engineer",
        "AI Backend Engineer",
        "Junior AI Developer",
        "LLM Application Engineer",
        "Python AI Engineer",
        "Senior AI Engineer",
        "Remote AI Engineer",
    ]
    required = [
        ["Python", "RAG", "Docker"],
        ["Python", "RAG", "FastAPI"],
        ["Python", "PyTorch", "scikit-learn"],
        ["Python", "RAG", "SQL"],
        ["Python", "RAG", "Pydantic"],
        ["Python", "PyTorch", "SQL"],
        ["Python", "FastAPI", "Docker"],
        ["Python", "RAG", "AWS"],
        ["Python", "RAG", "Docker"],
        ["Python", "Docker", "SQL"],
        ["Python", "Kubernetes", "Spark"],
        ["Python", "RAG", "Docker"],
    ]
    result = []
    for i, title in enumerate(titles):
        description = "Required skills: " + ", ".join(required[i]) + ". Preferred: AWS. "
        description += "5+ years experience." if i == 10 else "Entry-level candidates welcome."
        location = "Remote (United States only)" if i == 11 else "Cairo, Egypt"
        if i == 11:
            description += " Must be located in the United States."
        result.append(
            {
                "id": f"fixture-{i}",
                "title": title,
                "company": f"Demo Company {i + 1}",
                "location": location,
                "description": description,
                "url": "",
                "source": "Synthetic fixture",
                "retrieved_at": now,
                "mode": "synthetic",
            }
        )
    return result


def emails():
    now = datetime.now(UTC)
    return [
        {
            "id": "fixture-interview",
            "thread_id": "fixture-interview",
            "latest_message_id": "demo-1",
            "sender": "Recruiter <recruiter@example.invalid>",
            "subject": "Interview confirmation",
            "summary": "Synthetic recruiter invitation awaiting your confirmation.",
            "category": "interview",
            "requires_response": True,
            "response_state": "unanswered",
            "priority": 100,
            "deadline": (now + timedelta(days=1)).isoformat(),
            "deadline_evidence": "Synthetic fixture: please confirm by tomorrow.",
            "evidence": "Synthetic interview request, not a real email.",
            "uncertainty": "",
            "url": "",
            "mode": "synthetic",
            "reply_message_id": "",
            "received_at": now.isoformat(),
        },
        {
            "id": "fixture-recruiter",
            "thread_id": "fixture-recruiter",
            "latest_message_id": "demo-2",
            "sender": "Recruiter <talent@example.invalid>",
            "subject": "AI role availability",
            "summary": "Synthetic recruiter asks whether you would like role details.",
            "category": "recruiter",
            "requires_response": True,
            "response_state": "unanswered",
            "priority": 85,
            "deadline": None,
            "deadline_evidence": "",
            "evidence": "Would you like to receive the role description?",
            "uncertainty": "",
            "url": "",
            "mode": "synthetic",
            "reply_message_id": "",
            "received_at": (now - timedelta(days=4)).isoformat(),
        },
        {
            "id": "fixture-answered",
            "thread_id": "fixture-answered",
            "latest_message_id": "demo-3",
            "sender": "Recruiter <answered@example.invalid>",
            "subject": "Application received",
            "summary": "Synthetic application confirmation already acknowledged.",
            "category": "confirmation",
            "requires_response": False,
            "response_state": "responded",
            "priority": 20,
            "deadline": None,
            "deadline_evidence": "",
            "evidence": "Thanks for applying.",
            "uncertainty": "",
            "url": "",
            "mode": "synthetic",
            "reply_message_id": "",
            "received_at": now.isoformat(),
        },
    ]
