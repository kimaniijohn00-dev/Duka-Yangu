"""Check Muse Spark tool-calling with a Duka-Yangu transaction parse."""
import json
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.getenv("MODEL_API_KEY")
if not api_key:
    raise SystemExit("MODEL_API_KEY is not set. Add it to .env.")

client = OpenAI(
    base_url="https://api.meta.ai/v1",
    api_key=api_key,
)

tools = [
    {
        "type": "function",
        "function": {
            "name": "record_transaction",
            "description": "Record a shop transaction parsed from natural language.",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "sale",
                            "purchase",
                            "expense",
                            "credit_given",
                            "credit_received",
                        ],
                    },
                    "item": {"type": "string"},
                    "quantity": {"type": "number"},
                    "unit": {"type": "string"},
                    "amount_kes": {"type": "integer"},
                },
                "required": ["type", "item", "quantity", "unit", "amount_kes"],
                "additionalProperties": False,
            },
        },
    }
]

response = client.chat.completions.create(
    model="muse-spark-1.3",
    messages=[{"role": "user", "content": "sold 5 kilos of sugar for 800 bob"}],
    tools=tools,
    tool_choice="auto",
)

tool_calls = response.choices[0].message.tool_calls
if not tool_calls:
    raise SystemExit("Model did not return a tool call.")

parsed = json.loads(tool_calls[0].function.arguments)
print(json.dumps(parsed, indent=2))
