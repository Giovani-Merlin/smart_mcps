#!/usr/bin/env python3
"""
Perplexity CLI — bash-callable interface to the Perplexity AI API.

Subcommands:
  ask      <question>  Quick factual Q&A (sonar-pro, Sonar API)
  research <topic>     Deep multi-source investigation (sonar-deep-research)
  reason   <question>  Step-by-step reasoning (sonar-reasoning-pro)
  agent    <question>  Multi-step agentic search with tools (Agent API)

Usage:
  smart-mcps-perplexity ask "what is the MCP protocol?"
  smart-mcps-perplexity ask "explain this code" --file script.py
  smart-mcps-perplexity ask "summarize recent findings" --scientific-research
  smart-mcps-perplexity ask "fastmcp library" --domains github.com
  smart-mcps-perplexity research "best practices for FastMCP proxy design"
  smart-mcps-perplexity reason "should I use MCP or CLI for tool integration?"
  smart-mcps-perplexity reason "compare X vs Y for Z" --context-size high
  smart-mcps-perplexity agent "compare VRAM reduction techniques"
  smart-mcps-perplexity agent "rank these 5 GPU configs by cost/perf" --use-sandbox
  smart-mcps-perplexity agent "deep dive into X" --preset deep-research

Requires: PERPLEXITY_API_KEY environment variable
Output: answer text to stdout (citations stripped)
Saves: complete JSON to $CLAUDE_PROJECT_DIR/docs/research/perplexity/{subcommand}/ when set
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

_SONAR_BASE_URL = "https://api.perplexity.ai"
_SCIENTIFIC_DOMAINS = ["arxiv.org", "huggingface.co", "github.com"]
_CONTEXT_SIZES = ("low", "medium", "high")


def _save_result(subcommand: str, question: str, data: dict) -> None:
    """Save complete response JSON to $CLAUDE_PROJECT_DIR/docs/research/perplexity/{subcommand}/."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir:
        return
    out_dir = os.path.join(project_dir, "docs", "research", "perplexity", subcommand)
    try:
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.now()
        slug = re.sub(r"[^a-z0-9]+", "-", question.lower())[:60].strip("-")
        fname = ts.strftime("%Y-%m-%d_%H%M%S") + "_" + slug + ".json"
        fpath = os.path.join(out_dir, fname)
        data["saved_at"] = ts.isoformat()
        data["subcommand"] = subcommand
        data["question"] = question
        tmp = fpath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, fpath)
    except OSError:
        pass


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


def _get_agent_client():
    """Return a Perplexity SDK client for the Agent API."""
    try:
        from perplexity import Perplexity
    except ImportError:
        print(
            "error: perplexityai package not installed. Run: pip install perplexityai",
            file=sys.stderr,
        )
        sys.exit(1)
    return Perplexity(api_key=_get_api_key())


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


def _build_agent_tools(args) -> list[dict] | None:
    """Build the tools array only when the user adds overrides beyond preset defaults.

    Returns None when no overrides are present so the preset's tool config is used as-is.
    """
    filters: dict = {}
    domain_filter = _build_domain_filter(args)
    if domain_filter:
        filters["search_domain_filter"] = domain_filter
    if getattr(args, "search_recency", None):
        filters["search_recency_filter"] = args.search_recency

    has_overrides = bool(filters) or args.use_sandbox or args.max_urls != 5
    if not has_overrides:
        return None

    tools: list[dict] = [{"type": "web_search"}]
    if filters:
        tools[0]["filters"] = filters
    tools.append({"type": "fetch_url", "max_urls": args.max_urls})
    if args.use_sandbox:
        tools.append({"type": "sandbox"})
    return tools


def _cmd_agent(args) -> None:
    """Run a multi-step agentic search via the Perplexity Agent API.

    Endpoint: POST https://api.perplexity.ai/v1/agent  (SDK: client.responses.create)

    Arguments
    ---------
    question              The input prompt (positional).
    --file PATH           Prepend file contents wrapped in <file name="..."> XML tags.
    --preset              Agent preset — controls model, max_steps, search config, and
                          reasoning effort automatically:
                            fast-search            — fast, low search depth (1 step)
                            pro-search             — balanced depth/speed (3 steps, default)
                            deep-research          — thorough multi-step (10 steps)
                            advanced-deep-research — institutional-grade (10 steps, Claude)
    --max-urls N          Max URLs the fetch_url tool may retrieve per run,
                          1–10 (default: 5).
    --search-recency      Restrict web search results by age:
                            hour | day | week | month | year
    --scientific-research Seed domain filter with arxiv.org, huggingface.co,
                          github.com (stackable with --domains).
    --domains d1,d2,...   Custom domain allowlist; prefix a domain with - to
                          exclude it instead (e.g. -reddit.com).
    --use-sandbox         Enable code execution tool (OFF by default).
                          Use only when the task needs deterministic computation:
                          exact math, ranking tables, data processing, format
                          conversion. Adds ~$0.03/session and 2–4 s extra latency.
                          Unnecessary for text queries (facts, summaries,
                          explanations — the model handles those without it).
    --instructions TEXT   System-level instructions prepended to the agent run
                          (e.g. "Focus on scalability; prefer recent sources").

    Built-in tools always enabled: web_search, fetch_url.
    Optional tool (default OFF): sandbox — isolated Python execution container.
    """
    client = _get_agent_client()
    tools = _build_agent_tools(args)
    kwargs: dict = {
        "preset": args.preset,
        "input": _load_question(args.question, args.file),
    }
    if tools is not None:
        kwargs["tools"] = tools
    if args.instructions:
        kwargs["instructions"] = args.instructions
    response = client.responses.create(**kwargs)
    # response.output_text crashes when non-message output items have content=None
    # (sandbox and search result items). Extract text safely instead.
    texts = []
    for item in response.output:
        content = getattr(item, "content", None)
        if not content:
            continue
        for part in content:
            if getattr(part, "type", None) == "output_text":
                text = getattr(part, "text", "")
                if text:
                    texts.append(text)
    raw = "".join(texts)
    result = _strip_citations(raw)
    if not result:
        item_types = [getattr(o, "type", "?") for o in response.output]
        print(
            f"error: agent completed but produced no text output "
            f"(status={response.status}, output_types={item_types}). "
            f"Try --preset fast-search or add "
            f"--instructions 'Use at most 3 searches then write a comprehensive answer'.",
            file=sys.stderr,
        )
        sys.exit(1)
    _save_result(
        "agent",
        args.question,
        {
            "preset": args.preset,
            "status": response.status,
            "raw_text": raw,
            "answer": result,
        },
    )
    print(result)


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
    content = completion.choices[0].message.content
    citations = list(getattr(completion, "citations", None) or [])
    _save_result(
        "ask",
        args.question,
        {
            "model": completion.model,
            "raw_text": content,
            "answer": _strip_citations(content),
            "citations": citations,
            "usage": completion.usage.model_dump() if completion.usage else None,
        },
    )
    print(_strip_citations(content))


def _cmd_research(args) -> None:
    """Run a deep multi-source investigation via sonar-deep-research (slow, expensive)."""
    client = _get_sonar_client()
    completion = client.chat.completions.create(
        model="sonar-deep-research",
        messages=_build_messages(_load_question(args.topic, args.file)),
        extra_body={"search_context_size": args.context_size},
    )
    content = completion.choices[0].message.content
    citations = list(getattr(completion, "citations", None) or [])
    _save_result(
        "research",
        args.topic,
        {
            "model": completion.model,
            "raw_text": content,
            "answer": _strip_citations(content),
            "citations": citations,
            "usage": completion.usage.model_dump() if completion.usage else None,
        },
    )
    print(_strip_citations(content))


def _cmd_reason(args) -> None:
    """Run a step-by-step chain-of-thought analysis via sonar-reasoning-pro."""
    client = _get_sonar_client()
    completion = client.chat.completions.create(
        model="sonar-reasoning-pro",
        messages=_build_messages(_load_question(args.question, args.file)),
        extra_body={"search_context_size": args.context_size},
    )
    content = completion.choices[0].message.content
    citations = list(getattr(completion, "citations", None) or [])
    _save_result(
        "reason",
        args.question,
        {
            "model": completion.model,
            "raw_text": content,
            "answer": _strip_citations(content),
            "citations": citations,
            "usage": completion.usage.model_dump() if completion.usage else None,
        },
    )
    print(_strip_citations(content))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="smart-mcps-perplexity",
        description="Perplexity CLI — web-grounded AI via the Perplexity API",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # agent
    p_agent = sub.add_parser(
        "agent",
        help="Multi-step agentic search with tools: web_search, fetch_url, sandbox (Agent API)",
    )
    p_agent.add_argument("question")
    p_agent.add_argument(
        "--file",
        default=None,
        metavar="PATH",
        help="Prepend file contents to the question",
    )
    p_agent.add_argument(
        "--preset",
        choices=["fast-search", "pro-search", "deep-research", "advanced-deep-research"],
        default="pro-search",
        help="Agent preset controlling model, steps, and search config (default: pro-search)",
    )
    p_agent.add_argument(
        "--max-urls",
        type=int,
        default=5,
        metavar="N",
        help="Max URLs fetched by fetch_url tool, 1–10 (default: 5)",
    )
    p_agent.add_argument(
        "--search-recency",
        choices=["hour", "day", "week", "month", "year"],
        default=None,
        help="Restrict search results by recency",
    )
    p_agent.add_argument(
        "--scientific-research",
        action="store_true",
        help=f"Restrict search to {', '.join(_SCIENTIFIC_DOMAINS)}",
    )
    p_agent.add_argument(
        "--domains",
        default=None,
        help="Comma-separated domain allowlist, prefix with - to exclude",
    )
    p_agent.add_argument(
        "--use-sandbox",
        action="store_true",
        help="Enable code execution for math, ranking, data processing (OFF by default)",
    )
    p_agent.add_argument(
        "--instructions",
        default=None,
        help="System-level instructions for the agent",
    )

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
    p_reason = sub.add_parser("reason", help="Step-by-step reasoning (sonar-reasoning-pro)")
    p_reason.add_argument("question")
    p_reason.add_argument(
        "--file",
        default=None,
        metavar="PATH",
        help="Prepend file contents to the question",
    )
    _add_context_size_arg(p_reason)

    args = parser.parse_args()

    if args.cmd == "agent":
        _cmd_agent(args)
    elif args.cmd == "ask":
        _cmd_ask(args)
    elif args.cmd == "research":
        _cmd_research(args)
    elif args.cmd == "reason":
        _cmd_reason(args)


if __name__ == "__main__":
    main()
