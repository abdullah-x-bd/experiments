#!/usr/bin/env python3
"""One full-manuscript JGLR diagnostic, unrestricted adaptive MAX reasoning."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from review_run import extract_docx, lines, multimodal_content

MODEL = "anthropic/claude-fable-5.2"
HARD_TOTAL_USD = 7.75
PRIOR_USD = 2.706568
INPUT_RESERVE_USD = 0.55
OUTPUT_PRICE_PER_MILLION = 40.0

SYSTEM = (
    "You are an exceptionally demanding anonymous peer reviewer for Jindal Global Law Review "
    "(JGLR), combining comparative private law, professional liability, AI governance, "
    "legal doctrine, law-and-economics and legal editing expertise. Read the WHOLE attached "
    "manuscript, all numbered paragraphs, footnotes and the figure before making any final "
    "assessment. Analyze it as ONE interconnected argument, not as separate paragraph batches. "
    "Think as deeply as necessary. Produce a comprehensive, adversarial DIAGNOSTIC REPORT; "
    "do not rewrite the article. Evaluate originality against the literature described, "
    "the coherence and necessity of exclusive channelling, alternative liability systems, "
    "the five statutory predicates, professional duties, mandatory supplier recourse, "
    "evidentiary and remedial design, incentives, claimant access, comparative methodology, "
    "US/EU/Indian doctrine, procedural feasibility, financial risk, institutional competence, "
    "and whether the India/Global South contribution genuinely advances JGLR's mission. "
    "Identify unsupported or overstated claims, missing counterarguments, contradictions, "
    "incorrect or unclear legal characterisations, gaps in proof, duplicate material, "
    "footnote and OSCOLA issues, and important sentence/word-level imprecision. "
    "For every substantive criticism, give an exact quoted passage with its paragraph ID, "
    "severity (critical/major/minor/editorial), the legal or intellectual explanation, "
    "a strongest objection and a specific correction direction. Cover title, abstract, "
    "all sections, conclusion, terminology, citations and figure. Distinguish existing "
    "enacted law from proposals, drafts and assumptions. No browsing tools are provided, "
    "so mark facts and authorities requiring independent verification; never invent a "
    "source or claim to have verified it. Focus on real diagnostic substance, not praise. "
    "Conclude with a ranked publication-threatening issues list and revision priorities."
)
USER_PREFIX = (
    "Review the entire following DOCX-based manuscript in a SINGLE INTEGRATED "
    "referee report for JGLR, with no paragraph-batch division and no word-limit "
    "target. Preserve precise paragraph IDs for citations. Every substantive "
    "argument, significant footnote, and disputed claim should be scrutinised. "
    "The full manuscript begins below:\n\n"
)

def run():
    p = argparse.ArgumentParser()
    p.add_argument("--file", type=Path, default=Path("input/manuscript.docx"))
    p.add_argument("--out", type=Path, default=Path("results"))
    p.add_argument("--budget", type=float, default=HARD_TOTAL_USD)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    allowed = min(args.budget, HARD_TOTAL_USD)
    available = allowed - PRIOR_USD
    if available <= INPUT_RESERVE_USD + 0.25:
        raise SystemExit("Insufficient remaining allocation; no request sent")
    # Output budget includes reasoning AND final text; reasoning itself has no separate cap.
    max_output = min(110000, int((available - INPUT_RESERVE_USD) * 1_000_000 /
                                 OUTPUT_PRICE_PER_MILLION))
    if max_output < 12000:
        raise SystemExit("Insufficient completion allowance; no request sent")
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise SystemExit("Missing OPENROUTER_API_KEY")
    paragraphs, media = extract_docx(args.file)
    prompt = USER_PREFIX + lines(paragraphs)
    figures = [image for paragraph in paragraphs for image in paragraph["images"]]
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": multimodal_content(prompt, figures, media)},
        ],
        "reasoning": {"effort": "max", "exclude": True},
        "max_completion_tokens": max_output,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    summary = {
        "model": MODEL, "budget_usd_total": allowed, "previous_spend_usd": PRIOR_USD,
        "available_before_request_usd": available, "max_completion_tokens": max_output,
        "reasoning_effort": "max", "reasoning_token_limit": None,
        "paragraphs_in_full_request": len(paragraphs),
        "images_in_full_request": len(set(figures)),
        "status": "requesting",
    }
    (args.out / "status.json").write_text(json.dumps(summary, indent=2))
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Title": "Internal model experiment",
        }, method="POST"
    )
    response_path = args.out / "full_manuscript_review.md"
    text_chars = 0
    usage = {}
    finish_reason = None
    errors = []
    response_path.write_text("# Full-manuscript diagnostic review\n\n", encoding="utf-8")
    print("Starting ONE complete-manuscript MAX review. Total available: USD",
          round(available, 6), "Output allowance:", max_output, flush=True)
    try:
        with urllib.request.urlopen(request, timeout=3600) as response:
            with response_path.open("a", encoding="utf-8") as output:
                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if event.get("error"):
                        errors.append(str(event["error"])[:1000])
                        break
                    if event.get("usage"):
                        usage = event["usage"]
                    for choice in event.get("choices") or []:
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        delta = choice.get("delta") or {}
                        part = delta.get("content") or ""
                        if isinstance(part, list):
                            part = "".join(x.get("text", "") for x in part
                                           if isinstance(x, dict))
                        if isinstance(part, str) and part:
                            output.write(part)
                            text_chars += len(part)
                            if text_chars % 2000 < len(part):
                                output.flush()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        details = str(exc)
        if isinstance(exc, urllib.error.HTTPError):
            details += " " + exc.read().decode("utf-8", "replace")[:2000]
        errors.append(details)
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc))
    charged = usage.get("cost")
    if charged is None and usage:
        charged = (
            int(usage.get("prompt_tokens") or 0) * 8.0 / 1_000_000
            + int(usage.get("completion_tokens") or 0) * 40.0 / 1_000_000
        )
    summary.update({
        "status": "completed" if text_chars >= 500 and not errors else "failed",
        "visible_characters": text_chars, "finish_reason": finish_reason,
        "usage": usage, "charged_usd_this_request": charged,
        "cumulative_spend_usd": PRIOR_USD + charged if charged is not None else None,
        "errors": errors,
    })
    (args.out / "status.json").write_text(json.dumps(summary, indent=2))
    print("Review status:", summary["status"], "characters:", text_chars,
          "finish_reason:", finish_reason, "reported cost:", charged, flush=True)
    if errors or text_chars < 500:
        raise SystemExit("Full review did not produce an adequate visible report. "
                         "See status.json and retained partial text.")

if __name__ == "__main__":
    run()
