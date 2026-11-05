"""Claude list prices in US dollars per million tokens, used to report cost per ticket.

Anthropic first party API rates, checked on 4 Nov 2026. Claude Haiku 5.5 is
priced for prompts up to 100K tokens, which every triage prompt is. Update this
table when prices change. A model that is not listed is reported without a cost.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1_000_000


PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(0.10, 0.50),
    "claude-sonnet-5-5": Price(2.00, 10.00),
    "claude-opus-5-5": Price(4.00, 20.00),
    "claude-fable-5-1": Price(10.00, 50.00),
    "claude-haiku-4-5": Price(1.00, 5.00),
    "claude-sonnet-4-6": Price(3.00, 15.00),
}


def price_for(model: str | None) -> Price | None:
    return PRICES.get(model or "")
