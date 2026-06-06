#!/usr/bin/env python3
"""
Perplexity CLI — bash-callable interface to the Perplexity AI API.

Subcommands:
  ask      <question>  Quick factual Q&A (sonar-pro, Sonar API)
  research <topic>     Deep multi-source investigation (sonar-deep-research)
  reason   <question>  Step-by-step reasoning (sonar-reasoning-pro)

Usage:
  smart-mcps-perplexity ask "what is the MCP protocol?"
  smart-mcps-perplexity ask "explain this code" --file script.py
  smart-mcps-perplexity ask "summarize recent findings" --scientific-research
  smart-mcps-perplexity ask "agentmemory lib" --domains github.com
  smart-mcps-perplexity research "best practices for FastMCP proxy design"
  smart-mcps-perplexity reason "should I use MCP or CLI for tool integration?"
  smart-mcps-perplexity reason "compare X vs Y for Z" --context-size high

Requires: PERPLEXITY_API_KEY environment variable
Output: answer text to stdout (citations stripped)
"""

import argparse
import os
import re
import sys

_SONAR_BASE_URL = "https://api.perplexity.ai"
_SCIENTIFIC_DOMAINS = ["arxiv.org", "huggingface.co", "github.com"]
_CONTEXT_SIZES = ("low", "medium", "high")


def _get_api_key() -> str:
    """Read PERPLEXITY_API_KEY from the environment, exit with an error if missing."""
    api_key = os.environ.get("PERPLEXITY_API_KEY")
    if not api_key:
        print("error: PERPLEXITY_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)
    return api_key


def _get_sonar_client():
    """Return an OpenAI-compatible client pointed at the Perplexity Sonar API."""
    try:
        from openai import OpenAI
    except ImportError:
        print(
            "error: openai package not installed. Run: pip install openai",
            file=sys.stderr,
        )
        sys.exit(1)
    return OpenAI(api_key=_get_api_key(), base_url=_SONAR_BASE_URL)


def _build_messages(question: str, system: str | None = None) -> list[dict]:
    """Construct a chat messages list, optionally prepending a system prompt."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": question})
    return msgs


def _strip_citations(text: str) -> str:
    """Remove inline citation markers like [1], [2][3] from the model's response.

    Perplexity embeds numeric references throughout the answer text. Stripping them
    keeps the output clean and avoids inflating context when the response is fed back
    into another model.
    """
    return re.sub(r"(\[\d+\])+", "", text).strip()


def _print_sonar_completion(completion) -> None:
    """Print the text content of a Sonar completion with citations stripped."""
    print(_strip_citations(completion.choices[0].message.content))


def _load_question(question: str, file_path: str | None) -> str:
    """Build the final prompt, optionally prepending a file's contents.

    When --file is given the file content is wrapped in an XML-style tag so the
    model clearly separates document context from the user's question:

        <file name="script.py">
        ...file contents...
        </file>

        <user question>
    """
    if not file_path:
        return question
    try:
        with open(file_path, encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        print(f"error: cannot read file {file_path}: {e}", file=sys.stderr)
        sys.exit(1)
    name = os.path.basename(file_path)
    return f'<file name="{name}">\n{content}\n</file>\n\n{question}'


def _add_context_size_arg(parser: argparse.ArgumentParser) -> None:
    """Attach the shared --context-size flag controlling search_context_size.

    Cost and latency scale with retrieval breadth: medium suits most technical
    questions, high is for broad/high-stakes investigations, low for narrow
    factual lookups.
    """
    parser.add_argument(
        "--context-size",
        choices=_CONTEXT_SIZES,
        default="medium",
        help="Search retrieval breadth passed to the API (default: medium)",
    )


def _build_domain_filter(args) -> list[str] | None:
    """Collect the search_domain_filter list from CLI flags.

    --scientific-research seeds the list with arxiv/huggingface/github.
    --domains appends any user-supplied domains on top.
    Returns None when no filter is requested so the caller can skip the field.
    """
    domains: list[str] = []
    if getattr(args, "scientific_research", False):
        domains.extend(_SCIENTIFIC_DOMAINS)
    if getattr(args, "domains", None):
        domains.extend([d.strip() for d in args.domains.split(",")])
    return domains or None


def _cmd_ask(args) -> None:
    """Run a quick factual Q&A against sonar-pro with web search."""
    client = _get_sonar_client()
    extra: dict = {"search_context_size": args.context_size}
    domain_filter = _build_domain_filter(args)
    if domain_filter:
        extra["search_domain_filter"] = domain_filter
    completion = client.chat.completions.create(
        model="sonar-pro",
        messages=_build_messages(_load_question(args.question, args.file)),
        extra_body=extra,
    )
    _print_sonar_completion(completion)


def _cmd_research(args) -> None:
    """Run a deep multi-source investigation via sonar-deep-research (slow, expensive)."""
    client = _get_sonar_client()
    completion = client.chat.completions.create(
        model="sonar-deep-research",
        messages=_build_messages(_load_question(args.topic, args.file)),
        extra_body={"search_context_size": args.context_size},
    )
    _print_sonar_completion(completion)


def _cmd_reason(args) -> None:
    """Run a step-by-step chain-of-thought analysis via sonar-reasoning-pro."""
    client = _get_sonar_client()
    completion = client.chat.completions.create(
        model="sonar-reasoning-pro",
        messages=_build_messages(_load_question(args.question, args.file)),
        extra_body={"search_context_size": args.context_size},
    )
    _print_sonar_completion(completion)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="smart-mcps-perplexity",
        description="Perplexity CLI — web-grounded AI via the Perplexity API",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ask
    p_ask = sub.add_parser("ask", help="Quick factual Q&A with web search (sonar-pro)")
    p_ask.add_argument("question")
    p_ask.add_argument(
        "--file",
        default=None,
        metavar="PATH",
        help="Prepend file contents to the question (supports any text/code/markdown file)",
    )
    p_ask.add_argument(
        "--scientific-research",
        action="store_true",
        help=f"Restrict search to {', '.join(_SCIENTIFIC_DOMAINS)}",
    )
    p_ask.add_argument(
        "--domains",
        default=None,
        help="Comma-separated domain allowlist, prefix with - to exclude",
    )
    _add_context_size_arg(p_ask)

    # research
    p_research = sub.add_parser(
        "research", help="Deep multi-source investigation (slow, 30s+, expensive)"
    )
    p_research.add_argument("topic")
    p_research.add_argument(
        "--file",
        default=None,
        metavar="PATH",
        help="Prepend file contents to the topic",
    )
    _add_context_size_arg(p_research)

    # reason
    p_reason = sub.add_parser(
        "reason", help="Step-by-step reasoning (sonar-reasoning-pro)"
    )
    p_reason.add_argument("question")
    p_reason.add_argument(
        "--file",
        default=None,
        metavar="PATH",
        help="Prepend file contents to the question",
    )
    _add_context_size_arg(p_reason)

    args = parser.parse_args()

    if args.cmd == "ask":
        _cmd_ask(args)
    elif args.cmd == "research":
        _cmd_research(args)
    elif args.cmd == "reason":
        _cmd_reason(args)


if __name__ == "__main__":
    main()
