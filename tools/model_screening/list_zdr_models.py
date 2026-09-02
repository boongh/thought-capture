"""List OpenRouter's live zero-data-retention endpoints as screening candidates.

Fetches ``GET /api/v1/endpoints/zdr`` (the same live, authenticated source
every round of ``docs/model-evaluation-organize-select.md`` used), filters to
plausible candidates, and writes a JSON file shaped for
``screen_candidate.py --candidates-file``. Re-run this whenever you want a
fresh list - the ZDR endpoint set changes over time (830 entries as of
2026-09-02, 832 a day later), so a stale snapshot is a real risk, not a
theoretical one.

Filters applied (defaults match round 3/4's widened, "can go up in price a
bit" band - override with flags for a different pass):

- Not already recorded in ``docs/model-screening/results.md`` (unless
  ``--include-tested`` is given) - this is what makes "list only what's
  still worth trying" (model-candidate-screening skill, step 1) possible.
- Not an embedding/rerank/guard/moderation/voxtral/tts/whisper model by name
  - these can never pass an organize/select battery regardless of price or
  ZDR status, and the live ZDR list includes plenty of them.
- Advertises ``structured_outputs`` or ``response_format`` in its supported
  parameters. This is a hint, not a guarantee (round 3's
  ``llama-3.3-70b-instruct`` advertised it and still 404'd on every ZDR tag) -
  ``screen_candidate.py``'s capability pre-check is the real check.
- Prompt/completion price under the given ceilings, context above the given
  floor.
- Model id does not match a reasoning-branded naming pattern (round 2's
  DeepSeek finding, round 7's GLM/MiMo findings: hidden reasoning tokens can
  dominate cost and latency, or consume the entire budget with no visible
  output, independent of price-per-token). This is a heuristic on the name
  only - it will both over- and under-exclude - not a substitute for reading
  ``supported_parameters`` for a ``reasoning`` entry.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import zdr_endpoints_client

REASONING_HINTS = ("reasoner", "-r1", "thinking", ":think", "-o1", "-o3", "-o4", "qwq")
# Not chat/completion models at all - no organize/select battery can ever
# pass against these regardless of price or ZDR status.
NON_CHAT_HINTS = ("embedding", "rerank", "guard", "moderation", "voxtral", "tts", "whisper")

RESULTS_TABLE_MODEL_ID_RE = re.compile(r"`([a-zA-Z0-9_.\-]+/[a-zA-Z0-9_.\-]+)`")


def already_tested_model_ids(results_path: Path) -> set[str]:
    if not results_path.exists():
        return set()
    text = results_path.read_text(encoding="utf-8")
    ids: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 5:
            continue
        # Model id is always the first backtick-quoted cell on a data row.
        match = RESULTS_TABLE_MODEL_ID_RE.search(cells[4] if len(cells) > 4 else "")
        if match:
            ids.add(match.group(1))
    return ids


def fetch_zdr_rows() -> list[dict]:
    with zdr_endpoints_client() as client:
        response = client.get("/endpoints/zdr")
        response.raise_for_status()
        payload = response.json()
    return payload.get("data", payload if isinstance(payload, list) else [])


def filter_candidates(
    rows: list[dict],
    *,
    exclude_ids: set[str],
    max_prompt_price: float,
    max_completion_price: float,
    min_context: int,
    exclude_reasoning_hinted: bool,
) -> list[dict]:
    seen: dict[str, dict] = {}
    for row in rows:
        model_id = row.get("model_id")
        if not model_id or model_id in exclude_ids:
            continue
        if any(h in model_id.lower() for h in NON_CHAT_HINTS):
            continue
        if exclude_reasoning_hinted and any(h in model_id.lower() for h in REASONING_HINTS):
            continue
        params = row.get("supported_parameters") or []
        if "structured_outputs" not in params and "response_format" not in params:
            continue
        pricing = row.get("pricing") or {}
        try:
            prompt_price = float(pricing.get("prompt", "0")) * 1_000_000
            completion_price = float(pricing.get("completion", "0")) * 1_000_000
        except TypeError, ValueError:
            continue
        if prompt_price > max_prompt_price or completion_price > max_completion_price:
            continue
        context = row.get("context_length") or 0
        if context < min_context:
            continue
        candidate = {
            "model_id": model_id,
            "tag": row.get("tag"),
            "prompt_price_per_m": prompt_price,
            "completion_price_per_m": completion_price,
            "context_length": context,
            "uptime_last_1d": row.get("uptime_last_1d"),
            "supports_reasoning": "reasoning" in params,
        }
        existing = seen.get(model_id)
        if existing is None or prompt_price < existing["prompt_price_per_m"]:
            seen[model_id] = candidate
    return sorted(seen.values(), key=lambda c: c["prompt_price_per_m"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", required=True, help="path to write the candidate JSON list")
    parser.add_argument("--task", choices=["organize", "select"], default="organize")
    parser.add_argument(
        "--results-file",
        default="docs/model-screening/results.md",
        help="tracking table to exclude already-tested model ids from",
    )
    parser.add_argument("--include-tested", action="store_true")
    parser.add_argument(
        "--max-prompt-price", type=float, default=1.50, help="USD per M prompt tokens"
    )
    parser.add_argument(
        "--max-completion-price", type=float, default=5.00, help="USD per M completion tokens"
    )
    parser.add_argument("--min-context", type=int, default=32_000)
    parser.add_argument("--include-reasoning-hinted", action="store_true")
    parser.add_argument(
        "--limit", type=int, default=None, help="keep only the N cheapest after filtering"
    )
    args = parser.parse_args()

    exclude_ids = (
        set() if args.include_tested else already_tested_model_ids(Path(args.results_file))
    )
    rows = fetch_zdr_rows()
    candidates = filter_candidates(
        rows,
        exclude_ids=exclude_ids,
        max_prompt_price=args.max_prompt_price,
        max_completion_price=args.max_completion_price,
        min_context=args.min_context,
        exclude_reasoning_hinted=not args.include_reasoning_hinted,
    )
    if args.limit:
        candidates = candidates[: args.limit]

    print(f"{len(rows)} live ZDR endpoints, {len(exclude_ids)} already-tested model ids excluded")
    print(f"{len(candidates)} candidates after filtering:\n")
    for c in candidates:
        print(
            f"  {c['model_id']:<45} tag={c['tag']:<28} "
            f"${c['prompt_price_per_m']:.3f}/M in  ${c['completion_price_per_m']:.3f}/M out  "
            f"ctx={c['context_length']:>7}  reasoning={c['supports_reasoning']}"
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [{"model_id": c["model_id"], "tag": c["tag"], "task": args.task} for c in candidates]
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {len(payload)} candidates to {out_path}")


if __name__ == "__main__":
    main()
