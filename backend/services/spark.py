"""Muse Spark parsing service for Kenyan duka entries.

Reads MODEL_API_KEY from the repo-root .env. The key is only passed to the
OpenAI client - it is never printed or logged.
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

MODEL = "muse-spark-1.3"
BASE_URL = "https://api.meta.ai/v1"

SYSTEM_PROMPT = """You are the transaction parser for a Kenyan duka \
(corner shop) bookkeeping assistant. Convert the shopkeeper's message into \
one record_transaction call.

Rules:
- Amounts are Kenyan shillings (KES) as an integer in amount_kes.
- "bob" means shillings: "800 bob" is 800.
- "k" after a number means thousands: "2k" is 2000.
- Goods taken without paying now ("on credit", "deni", a customer name with \
no payment) means credit_given, with that person as counterparty.
- Money received to settle a past credit means credit_received.
- Cash received for goods right now means sale.
- Stock bought for the shop means purchase.
- Shop costs (rent, transport, bundles for the shop) mean expense.
- Quantity and unit come from phrases like "5 kilos"; when absent use \
quantity 1 and unit "pcs".
- Never invent values the message does not imply."""

TOOLS = [
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
                    "counterparty": {
                        "type": ["string", "null"],
                        "description": "Person involved in a credit transaction, if any.",
                    },
                },
                "required": ["type", "item", "quantity", "unit", "amount_kes"],
                "additionalProperties": False,
            },
        },
    }
]


def _get_client() -> OpenAI:
    root = Path(__file__).resolve().parent.parent.parent
    load_dotenv(root / ".env")
    api_key = os.getenv("MODEL_API_KEY")
    if not api_key:
        raise RuntimeError("MODEL_API_KEY is not set. Add it to .env.")
    return OpenAI(base_url=BASE_URL, api_key=api_key)


def parse_entry(text: str) -> dict:
    """Parse free text into a record_transaction argument dict."""
    client = _get_client()
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        tools=TOOLS,
        tool_choice="auto",
    )
    tool_calls = response.choices[0].message.tool_calls
    if not tool_calls:
        raise RuntimeError("Model did not return a tool call.")
    parsed = json.loads(tool_calls[0].function.arguments)
    parsed["amount_kes"] = int(parsed["amount_kes"])
    return parsed
