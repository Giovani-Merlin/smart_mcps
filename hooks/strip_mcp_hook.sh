#!/usr/bin/env bash
# PostToolUse hook: strips noise from NotebookLM and Perplexity MCP responses.
# Injects cleaned response as additionalContext (JSON output) so Claude uses the
# lean version without citations/source noise. Always exits 0.

input=$(cat)

tool_name=$(printf '%s' "$input" | python3 -c "
import json, sys
print(json.load(sys.stdin).get('tool_name', ''))
" 2>/dev/null)

emit_context() {
    local tool="$2"
    printf '%s' "$1" | python3 -c "
import json, sys
tool = sys.argv[1] if len(sys.argv) > 1 else ''
data = sys.stdin.read().strip()
msg = '[Hook] Raw ' + tool + ' response above was filtered. Use ONLY the following — discard the raw response:\n' + data
print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': msg}}))
" "$tool" 2>/dev/null
}

case "$tool_name" in
  mcp__notebooklm__notebook_query|\
  mcp__notebooklm__cross_notebook_query|\
  mcp__notebooklm__notebook_query_status)
    cleaned=$(printf '%s' "$input" | python3 -c "
import json, sys
d = json.load(sys.stdin)
try:
    r = json.loads(d.get('tool_response', '{}'))
    keep = ('status', 'answer', 'conversation_id', 'error')
    print(json.dumps({k: r[k] for k in keep if k in r}, ensure_ascii=False))
except Exception as e:
    sys.stderr.write(str(e) + '\n')
" 2>/dev/null)
    [ -n "$cleaned" ] && emit_context "$cleaned" "$tool_name"
    ;;

  mcp__perplexity__perplexity_ask|\
  mcp__perplexity__perplexity_research|\
  mcp__perplexity__perplexity_reason|\
  mcp__perplexity__perplexity_search)
    cleaned=$(printf '%s' "$input" | python3 -c "
import json, re, sys
d = json.load(sys.stdin)
try:
    r = json.loads(d.get('tool_response', '{}'))
    out = {}
    if 'error' in r:
        out['error'] = r['error']
    if 'response' in r:
        text = r['response']
        text = re.sub(r'\n+Citations:\n(?:\[\d+\][^\n]*\n?)*', '', text)
        text = re.sub(r'\[\d+(?:,\s*\d+)*\]', '', text)
        text = re.sub(r'  +', ' ', text).strip()
        out['response'] = text
    print(json.dumps(out, ensure_ascii=False))
except Exception as e:
    sys.stderr.write(str(e) + '\n')
" 2>/dev/null)
    [ -n "$cleaned" ] && emit_context "$cleaned" "$tool_name"
    ;;
esac

exit 0
