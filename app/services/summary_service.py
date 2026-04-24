from openai import OpenAI

from app.infrastructure.config.settings import settings


def summarize_transcript_text(transcript_text: str) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    client = OpenAI(api_key=settings.openai_api_key)
    completion = client.chat.completions.create(
        model=settings.openai_model,
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an assistant that writes concise meeting "
                    "summaries with decisions and action focus. "
                    "The transcript includes speaker labels. "
                    "Keep speaker context in summary."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Create a short summary of this meeting transcript:\n\n"
                    f"{transcript_text}"
                ),
            },
        ],
    )
    content = completion.choices[0].message.content
    if content is None:
        raise RuntimeError("OpenAI returned empty summary")
    return content.strip()
