"""Q&A Assistant — Slack + GitHub Discussions. Directly answers what Jim
asked for in kyverno/kyverno#16665: "answer common questions using project
docs ... and link relevant issues/PRs, escalating to a human when
confidence is low."

Retrieval (`tools/doc_retriever.search_docs`) is LightRAG's hybrid
vector+graph ranking — a fixed algorithm, not an LLM judgment call. The
model chooses *what* to search for and drafts an answer, but two things are
never left to the model:

1. **Citations must be real.** `propose_answer`'s tool implementation
   tracks every `source_url` actually returned by `search_docs` during this
   run and rejects the call outright if `citations` is empty or names a URL
   never returned — "never answers without a real citation" as a Python
   guardrail, not a prompt instruction the model could ignore.
2. **Whether to post is a policy check, not the model's call.** After the
   run, plain Python compares the model's self-reported `confidence`
   against `config.qa_assistant.confidence_threshold`. Clears the bar ->
   post with citations. Doesn't clear it, or the model never proposed an
   answer at all -> escalate to a maintainer instead. Exactly the
   `merge_policy.py` split: the model explains, the policy check decides.
"""

from __future__ import annotations

import json

from claude_agent_sdk import (
    ClaudeAgentOptions,
    SandboxNetworkConfig,
    SandboxSettings,
    create_sdk_mcp_server,
    query,
    tool,
)
from loguru import logger

from src.agents._shared import memory_search, memory_write
from src.audit import AuditEntry
from src.config import kill_switch_engaged
from src.events import Event, register_handler
from src.memory import build_memory_tool_server
from src.runtime import (
    can_use_tool,
    get_audit_writer,
    get_config,
    get_github_token,
    get_memory_client,
    get_repo_variable,
    single_turn_prompt,
)
from src.skills import load_skill
from src.terminal import stream_agent_run
from src.tools.discussion_tools import add_discussion_comment
from src.tools.doc_retriever import search_docs
from src.tools.slack_tools import post_message

WORKFLOW = "qa_assistant"
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
_SLACK_SOURCES = {"slack", "app_mention", "slack_assistant"}
_DISCUSSION_SOURCES = {"github_discussion", "discussion_comment"}


def validate_citations(citations: list[str], seen_urls: set[str], confidence: str | None) -> str | None:
    """Pure function behind `propose_answer`'s guardrail — pulled out so the
    "never answers without a real citation" property is directly
    unit-testable (`tests/test_qa_assistant.py`) without going through the
    Claude Agent SDK's tool-call machinery. Returns an error message string
    if the proposal should be rejected, `None` if it's valid."""
    if not citations:
        return "REJECTED: citations must be non-empty. Call search_docs first, then cite only what it returned."
    unknown = [c for c in citations if c not in seen_urls]
    if unknown:
        return f"REJECTED: these citations were never returned by search_docs in this run: {unknown}"
    if confidence not in _CONFIDENCE_RANK:
        return "REJECTED: confidence must be exactly 'high', 'medium', or 'low'"
    return None


def decide_post_or_escalate(proposal: dict, confidence_threshold: str) -> tuple[str, str]:
    """Pure function behind the post-run policy gate — mirrors
    `merge_policy.evaluate`'s split: the model explains (via `proposal`),
    this decides. Returns `(decision, reason)` where `decision` is
    `"answer"` or `"escalate"`."""
    threshold_rank = _CONFIDENCE_RANK[confidence_threshold]
    proposed_rank = _CONFIDENCE_RANK.get(proposal.get("confidence"), -1)
    if proposal.get("answer") and proposed_rank >= threshold_rank:
        return "answer", f"confidence={proposal['confidence']} (>= threshold {confidence_threshold}), {len(proposal.get('citations', []))} citation(s)"
    if proposal.get("answer"):
        return "escalate", f"confidence={proposal.get('confidence')} below threshold {confidence_threshold}"
    return "escalate", "agent did not propose an answer"


def _build_qa_tool_server(config, seen_urls: set[str], proposal: dict):
    @tool(
        "search_docs",
        "Search kyverno.io docs and past resolved Q&A issues. Call this before answering — "
        "never answer from general knowledge alone.",
        {"query": str, "target_version": str},
    )
    async def search_docs_tool(args: dict) -> dict:
        chunks = await search_docs(
            args["query"],
            top_k=config.qa_assistant.max_search_results,
            target_version=args.get("target_version") or None,
        )
        for c in chunks:
            seen_urls.add(c.source_url)
        if not chunks:
            return {"content": [{"type": "text", "text": "No results found for this query."}]}
        listing = "\n\n".join(f"[{c.source_url}] (kyverno_version={c.kyverno_version})\n{c.text[:1000]}" for c in chunks)
        return {"content": [{"type": "text", "text": listing}]}

    @tool(
        "propose_answer",
        "Propose the final answer. citations must be source_url values that a prior "
        "search_docs call actually returned — anything else is rejected.",
        {"answer": str, "citations": list[str], "confidence": str},
    )
    async def propose_answer_tool(args: dict) -> dict:
        citations = args.get("citations") or []
        error = validate_citations(citations, seen_urls, args.get("confidence"))
        if error:
            return {"content": [{"type": "text", "text": error}], "is_error": True}
        proposal["answer"] = args["answer"]
        proposal["citations"] = citations
        proposal["confidence"] = args["confidence"]
        return {"content": [{"type": "text", "text": "answer recorded"}]}

    return create_sdk_mcp_server(name="qa", tools=[search_docs_tool, propose_answer_tool])


def _format_answer(answer: str, citations: list[str]) -> str:
    sources = "\n".join(f"- {c}" for c in citations)
    return f"{answer}\n\nSources:\n{sources}"


def _post_answer(source: str, thread_ref: str, text: str) -> bool:
    if source in _SLACK_SOURCES:
        channel, _, thread_ts = thread_ref.partition(":")
        return post_message(channel, text, thread_ts=thread_ts or None)
    if source in _DISCUSSION_SOURCES:
        return add_discussion_comment(thread_ref, text, token=get_github_token())
    logger.warning(f"_post_answer: unrecognized source {source!r}, not posting")
    return False


def _escalate(config, source: str, thread_ref: str, question_text: str, reason: str) -> bool:
    esc = config.qa_assistant.escalation
    if source in _SLACK_SOURCES:
        post_message(esc.slack_channel, f"Q&A bot couldn't confidently answer (source={source}): {question_text!r}\nReason: {reason}")
        channel, _, thread_ts = thread_ref.partition(":")
        return post_message(
            channel,
            "I'm not confident enough in an answer here to post one automatically — flagging this for a maintainer.",
            thread_ts=thread_ts or None,
        )
    if source in _DISCUSSION_SOURCES:
        mentions = " ".join(f"@{login}" for login in esc.github_maintainer_logins) or "(no maintainer logins configured yet)"
        return add_discussion_comment(
            thread_ref,
            f"I couldn't answer this confidently enough to post automatically ({reason}). {mentions}, could you take a look?",
            token=get_github_token(),
        )
    logger.warning(f"_escalate: unrecognized source {source!r}, not escalating")
    return False


async def answer_question(
    question_text: str,
    source: str,
    thread_ref: str,
    external_id: str,
    parent_run_id: int | None = None,
) -> AuditEntry:
    config = get_config()
    audit = get_audit_writer()

    if kill_switch_engaged(config, get_repo_variable):
        return audit.write(
            trigger_event=source,
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="skipped",
            decision_reason="kill switch engaged",
            action_taken="none",
            action_result="skipped: kill switch engaged",
            parent_run_id=parent_run_id,
        )

    if not config.workflow_enabled(WORKFLOW):
        return audit.write(
            trigger_event=source,
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="skipped",
            decision_reason="workflow disabled in config",
            action_taken="none",
            action_result="skipped: workflow disabled",
            parent_run_id=parent_run_id,
        )

    seen_urls: set[str] = set()
    proposal: dict = {}
    tool_server = _build_qa_tool_server(config, seen_urls, proposal)
    skill = load_skill("qa-assistant")

    # Dimension 3 — see the matching comment in agents/dependabot.py.
    # Everything written to shared memory comes from already-public GitHub
    # activity (security_agent.py is the one deliberate exception — see its
    # module docstring), so reading it here doesn't open a new leak path;
    # it's just never a citable source (see the prompt below).
    memory = get_memory_client()
    memory_facts = await memory_search(question_text)
    mcp_servers = {"qa": tool_server}
    if memory is not None:
        mcp_servers["memory"] = build_memory_tool_server(memory, default_limit=config.memory.search_top_k)

    options = ClaudeAgentOptions(
        system_prompt=skill,
        mcp_servers=mcp_servers,
        tools=[],
        allowed_tools=[],
        can_use_tool=can_use_tool,
        max_turns=6,
        sandbox=SandboxSettings(
            enabled=True,
            # api.anthropic.com for the agent loop itself; api.voyageai.com
            # for search_docs's query-time embedding call (LightRAG's
            # embedding_func — see tools/doc_retriever.py). Slack/Discussions
            # posting happens in Python after the run, not as a sandboxed
            # tool call, so those domains don't need to be listed here.
            network=SandboxNetworkConfig(allowedDomains=["api.anthropic.com", "api.voyageai.com"]),
        ),
    )

    prompt = (
        f"Question (via {source}): {question_text!r}\n\n"
        f"Relevant memory (past questions/issues that might be related — background context "
        f"only, never a citable source): {memory_facts or 'none found'}\n\n"
        f"Search before answering — call `search_docs` with one or more queries. If you find "
        f"something relevant, call `propose_answer` with a specific answer, the exact "
        f"source_url(s) you're citing, and an honest confidence level. If nothing relevant "
        f"turns up, or you're not genuinely confident, do NOT call `propose_answer` at all — "
        f"just explain in your final message why you couldn't answer. Never answer from "
        f"general knowledge alone; only from what search_docs actually returned — memory facts "
        f"can help you judge relevance or spot a duplicate question, but citations must always "
        f"be real search_docs source_urls, never a memory fact."
    )

    result = await stream_agent_run(
        query(prompt=single_turn_prompt(prompt), options=options),
        title=f"Q&A Assistant — {source}",
    )

    gate_decision, decision_reason = decide_post_or_escalate(proposal, config.qa_assistant.confidence_threshold)

    if gate_decision == "answer":
        answer_text = _format_answer(proposal["answer"], proposal["citations"])
        posted = _post_answer(source, thread_ref, answer_text)
        decision = "answered"
        action_taken = "post_answer"
    else:
        posted = _escalate(config, source, thread_ref, question_text, decision_reason)
        decision = "escalated"
        action_taken = "escalate_to_maintainer"

    action_result = "success" if posted else "failed: posting/escalation call did not succeed"
    if result is None:
        action_result = "failed: no ResultMessage from agent run"
    elif result.is_error:
        action_result = f"failed: {result.subtype}"

    memory_refs = await memory_write(
        name=f"qa:{source}:{external_id}",
        episode_body=(
            f"Q&A question (via {source}): {question_text!r}. Decision: {decision} "
            f"({decision_reason}). Agent action result: {action_result}."
        ),
        source_description=WORKFLOW,
    )

    return audit.write(
        trigger_event=source,
        external_id=external_id,
        workflow_name=WORKFLOW,
        agent_decision=decision,
        decision_reason=decision_reason,
        agent_reasoning_summary=result.result if result else None,
        action_taken=action_taken,
        action_result=action_result,
        can_be_reverted=True,
        revert_command="delete the bot's reply/comment",
        parent_run_id=parent_run_id,
        total_cost_usd=result.total_cost_usd if result else None,
        duration_ms=result.duration_ms if result else None,
        memory_refs=json.dumps(memory_refs) if memory_refs else None,
    )


@register_handler("app_mention")
async def handle_app_mention(event: Event) -> None:
    """Slack Bolt's `@app.event("app_mention")` (see `slack_app.py`)
    converts the raw Slack event into this `Event` before dispatch — the
    fields below match what that adapter sets on `payload`."""
    text = event.payload.get("text", "")
    channel = event.payload.get("channel")
    thread_ts = event.payload.get("thread_ts") or event.payload.get("ts")
    if not channel or not thread_ts:
        logger.warning("app_mention event missing channel/ts, skipping")
        return
    await answer_question(text, "slack", f"{channel}:{thread_ts}", event.external_id)


@register_handler("discussion")
async def handle_discussion_created(event: Event) -> None:
    """Someone asking a question by opening a *new* Discussion (the natural
    way most people actually use GitHub Discussions Q&A) — distinct from
    `handle_discussion_comment` below, which answers a follow-up *reply* on
    an existing discussion. Both funnel into the same `answer_question()`,
    same `github_discussion` source/escalation path; this one was missing
    entirely until it was noticed that a real `discussion.created` webhook
    delivery had no registered handler at all and silently did nothing."""
    if event.action != "created":
        return
    discussion = event.payload.get("discussion", {})
    body = discussion.get("body", "")
    node_id = discussion.get("node_id")
    if not node_id or not body:
        logger.warning("discussion event missing discussion node_id/body, skipping")
        return
    await answer_question(body, "github_discussion", node_id, event.external_id)


@register_handler("discussion_comment")
async def handle_discussion_comment(event: Event) -> None:
    payload = event.payload
    comment = payload.get("comment", {})
    discussion = payload.get("discussion", {})
    body = comment.get("body", "")
    node_id = discussion.get("node_id")
    if not node_id or not body:
        logger.warning("discussion_comment event missing discussion node_id/body, skipping")
        return
    await answer_question(body, "github_discussion", node_id, event.external_id)
