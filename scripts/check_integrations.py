"""Read-only smoke check. Never prints credentials or email bodies."""

import argparse
import asyncio
import json

from pydantic import BaseModel

from app.config import Settings
from app.errors import AppError
from app.integrations.jobs import fetch_jobs
from app.integrations.llm import LLMClient


class SmokeOutput(BaseModel):
    category: str


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", action="store_true")
    parser.add_argument("--model", action="store_true")
    args = parser.parse_args()
    settings = Settings(mode="live")
    result = {"gmail": "Operator OAuth authorization required; not exercised."}
    if args.jobs:
        jobs, errors = await fetch_jobs(settings)
        result["jobs"] = {
            "count": len(jobs),
            "errors": errors,
            "examples": [{"title": j["title"], "url": j["url"]} for j in jobs[:2]],
        }
    if args.model:
        try:
            response = await LLMClient(settings).structured(
                SmokeOutput,
                "Classify the synthetic sentence as job or other.",
                "Example: A test interview invitation.",
            )
            result["model"] = response.model_dump()
        except AppError as error:
            result["model"] = {"error": error.code, "message": error.message}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
