#!/usr/bin/env python3
"""
Perplexity CLI — bash-callable interface to the Perplexity AI API.

Subcommands:
  ask      <question>  Quick factual Q&A with citations (sonar-pro)
  search   <query>     Ranked web search results (sonar)
  research <topic>     Deep multi-source investigation (sonar-deep-research)
  reason   <question>  Step-by-step reasoning (sonar-reasoning)

Usage:
  smart-mcps-perplexity ask "what is the MCP protocol?"
  smart-mcps-perplexity search "Claude Code plugin system" --num-results 5
  smart-mcps-perplexity research "best practices for FastMCP proxy design"
  smart-mcps-perplexity reason "should I use MCP or CLI for tool integration?"

Requires: PERPLEXITY_API_KEY environment variable
Output: answer text + citations to stdout
"""

import argparse
import json
import os
import sys


def _get_client():
    try:
        from perplexity import Perplexity
    except ImportError:
        print("error: perplexityai package not installed. Run: pip install perplexityai", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("PERPLEXITY_API_KEY")
    if not api_key:
        print("error: PERPLEXITY_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)

    return Perplexity(api_key=api_key)


def _build_messages(question: str, system: str | None = None) -> list[dict]:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": question})
    return msgs


def _print_completion(completion) -> None:
    content = completion.choices[0].message.content
    print(content)
    citations = getattr(completion, "citations", None)
    if citations:
        print("\nSources:")
        for i, url in enumerate(citations, 1):
            print(f"  [{i}] {url}")


def _cmd_ask(args) -> None:
    client = _get_client()
    extra: dict = {}
    if args.recency:
        extra["search_recency_filter"] = args.recency
    if args.domains:
        extra["search_domain_filter"] = [d.strip() for d in args.domains.split(",")]
    completion = client.chat.completions.create(
        model=args.model,
        messages=_build_messages(args.question),
        **extra,
    )
    _print_completion(completion)


def _cmd_search(args) -> None:
    client = _get_client()
    extra: dict = {}
    if args.domains:
        extra["search_domain_filter"] = [d.strip() for d in args.domains.split(",")]
    completion = client.chat.completions.create(
        model="sonar",
        messages=_build_messages(
            args.query,
            system="Return a concise answer with ranked sources. Focus on finding specific URLs and facts.",
        ),
        **extra,
    )
    _print_completion(completion)


def _cmd_research(args) -> None:
    client = _get_client()
    completion = client.chat.completions.create(
        model="sonar-deep-research",
        messages=_build_messages(args.topic),
    )
    _print_completion(completion)


def _cmd_reason(args) -> None:
    client = _get_client()
    completion = client.chat.completions.create(
        model="sonar-reasoning",
        messages=_build_messages(args.question),
    )
    _print_completion(completion)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="smart-mcps-perplexity",
        description="Perplexity CLI — web-grounded AI via the Perplexity API",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ask
    p_ask = sub.add_parser("ask", help="Quick factual Q&A with citations")
    p_ask.add_argument("question")
    p_ask.add_argument("--model", default="sonar-pro", choices=["sonar", "sonar-pro"])
    p_ask.add_argument("--recency", choices=["hour", "day", "week", "month", "year"], default=None)
    p_ask.add_argument("--domains", default=None, help="Comma-separated domain allowlist, prefix with - to exclude")

    # search
    p_search = sub.add_parser("search", help="Find URLs and recent facts")
    p_search.add_argument("query")
    p_search.add_argument("--domains", default=None, help="Comma-separated domain allowlist")

    # research
    p_research = sub.add_parser("research", help="Deep multi-source investigation (slow, 30s+)")
    p_research.add_argument("topic")

    # reason
    p_reason = sub.add_parser("reason", help="Step-by-step reasoning")
    p_reason.add_argument("question")

    args = parser.parse_args()

    try:
        if args.cmd == "ask":
            _cmd_ask(args)
        elif args.cmd == "search":
            _cmd_search(args)
        elif args.cmd == "research":
            _cmd_research(args)
        elif args.cmd == "reason":
            _cmd_reason(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
