"""Probe an OpenAI-compatible endpoint for models that work, including JSON mode.

python scripts/dev/probe_models.py
"""

from __future__ import annotations

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

CANDIDATES = [
    "qwen/qwen3.7-flash:free",
    "qwen/qwen3.5-flash:free",
    "qwen/qwen3.6-27b:free",
    "minimax/minimax-m2.7:free",
    "minimax/minimax-m2:free",
    "deepseek/deepseek-v4.1-flash:free",
]


def main() -> None:
    client = OpenAI()
    for name in CANDIDATES:
        for label, kwargs in (("text", {}), ("json", {"response_format": {"type": "json_object"}})):
            try:
                resp = client.chat.completions.create(
                    model=name,
                    temperature=0.0,
                    messages=[
                        {
                            "role": "system",
                            "content": "You are terse."
                            ' Reply as JSON {"ok": true} if asked for JSON.',
                        },
                        {"role": "user", "content": "Reply with ok."},
                    ],
                    **kwargs,
                )
                content = (resp.choices[0].message.content or "").strip().replace("\n", " ")
                print(f"  PASS {name:<38} {label:<5} {content[:60]}")
            except Exception as exc:
                reason = str(exc).split("\n")[0][:90]
                print(f"  FAIL {name:<38} {label:<5} {reason}")


if __name__ == "__main__":
    main()
