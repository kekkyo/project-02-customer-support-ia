"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # TODO comments.

Reference the step-by-step solution files and INSTRUCTIONS.md for guidance.
Do NOT copy the solution directly — work through each section yourself.

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ── TODO 1 — App Initialisation - DONE ───────────────────────────────────────────────

app = BedrockAgentCoreApp()


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── TODO 2 — Configuration - DONE ────────────────────────────────────────────────────

GATEWAY_URL = "https://customersupportgateway-kuk2p7e1if.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
MEMORY_ID = "CustomerSupportMemory-JbwtSF8GRT"
KB_ID = "XXZDDNISS6"   # pendiente, va al final
REGION = "us-east-1"


# ── TODO 3 — Model and Clients - DONE ────────────────────────────────────────────────

model_id = "global.amazon.nova-2-lite-v1:0"

model = BedrockModel(model_id=model_id)

memory_client = MemoryClient(region_name=REGION)

_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── TODO 4 — Namespace Helper - DONE ──────────────────────────────────────────────────

def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)
    result = {}
    for s in strategies:
        ns = s.get("namespaceTemplates") or s.get("namespaces")
        result[s["type"]] = ns[0]
    return result


# ── TODO 5 — Memory Hook - DONE ───────────────────────────────────────────────────────

class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_id = memory_id
        self.memory_client = memory_client
        self.namespaces = get_namespaces(memory_client, memory_id)

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        messages = event.agent.messages
        if not messages:
            return

        last_message = messages[-1]
        if last_message.get("role") != "user":
            return

        content = last_message.get("content", [])
        if not content or "text" not in content[0]:
            return
        if any("toolResult" in block for block in content):
            return

        query = content[0]["text"]

        context_parts = []
        for strategy_type, namespace_template in self.namespaces.items():
            namespace = namespace_template.format(actorId=self.actor_id)
            memories = self.memory_client.retrieve_memories(
                memory_id=self.memory_id,
                namespace=namespace,
                query=query,
                top_k=5,
            )
            for m in memories:
                text = m.get("content", {}).get("text", "")
                if text:
                    context_parts.append(f"[{strategy_type}] {text}")

        if context_parts:
            context_block = "Customer Context:\n" + "\n".join(context_parts)
            content[0]["text"] = f"{context_block}\n\n{query}"

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        messages = event.agent.messages

        customer_query = None
        agent_response = None

        for message in reversed(messages):
            content = message.get("content", [])
            if not content or "text" not in content[0]:
                continue
            if any("toolResult" in block for block in content):
                continue

            text = content[0]["text"]

            if message.get("role") == "assistant" and agent_response is None:
                agent_response = text
            elif message.get("role") == "user" and customer_query is None:
                customer_query = text

            if customer_query and agent_response:
                break

        if customer_query and agent_response:
            self.memory_client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")],
            )

    def register_hooks(self, registry: HookRegistry) -> None:
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)

# ── TODO 6 — Knowledge Base Tool - DONE ─────────────────────────────────────────────

@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID or not KB_ID.strip():
        return "Knowledge base is not configured. Please contact support for product, policy, or loyalty program questions."

    resp = _bedrock_runtime.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": query},
    )

    results = resp.get("retrievalResults", [])
    if not results:
        return "No relevant information found in the knowledge base."

    chunks = [r["content"]["text"] for r in results]
    return "\n---\n".join(chunks)


# ── TODO 7 — Loyalty Discount Tool (Code Interpreter) - DONE ────────────────────────

@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    # TODO: Build the code string (use an f-string to inject the arguments)
    code = f"""
    earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
    tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

    loyalty_points = {loyalty_points}
    tier = "{tier}"
    order_total = {order_total}
    product_category = "{product_category}"

    points_redeemed = min(int(loyalty_points // 500) * 500, int(order_total * 0.5 // 1))
    points_discount = points_redeemed * 0.01
    subtotal_after_points = order_total - points_discount

    tier_discount = subtotal_after_points * tier_rates.get(tier, 0.0)
    final_total = round(subtotal_after_points - tier_discount, 2)
    total_savings = round(order_total - final_total, 2)

    points_earned = int(order_total * earn_rates.get(product_category, 1))
    remaining_points = loyalty_points - points_redeemed + points_earned

    import json
    print(json.dumps({{
        "points_redeemed": points_redeemed,
        "tier_discount_pct": tier_rates.get(tier, 0.0),
        "tier_discount": round(tier_discount, 2),
        "final_total": final_total,
        "total_savings": total_savings,
        "points_earned": points_earned,
        "remaining_points": remaining_points
    }}))
    """

    try:
        with code_session(REGION) as session:
            response = session.invoke(
                "executeCode",
                {
                    "code": code,
                    "language": "python",
                    "clearContext": True,
                },
            )
            for event in response.get("stream", []):
                return json.dumps(event.get("result", event))

    except Exception as e:
        logger.warning(f"Code Interpreter unavailable, using fallback: {e}")
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        tier_discount = order_total * tier_rates.get(tier, 0.0)
        final_total = round(order_total - tier_discount, 2)
        return json.dumps({
            "fallback": True,
            "tier_discount_pct": tier_rates.get(tier, 0.0),
            "tier_discount": round(tier_discount, 2),
            "final_total": final_total,
            "note": "Code Interpreter unavailable — tier discount only, points not calculated",
        })


# ── TODO 8 — Agent Entrypoint - DONE ──────────────────────────────────────────────────

@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.
    ...
    """
    user_input = payload.get("prompt", "")
    actor_id = payload.get("customer_id", "default_customer")
    session_id = payload.get("session_id") or str(uuid.uuid4())

    memory_hook = MemoryHook(
        actor_id=actor_id,
        session_id=session_id,
        memory_client=memory_client,
        memory_id=MEMORY_ID,
    )

    agent_core_browser = AgentCoreBrowser(region=REGION)

    tools = [
        search_knowledge_base,
        calculate_loyalty_discount,
        agent_core_browser.browser,
    ]

    system_prompt = (
        "You are a helpful customer support agent for an online store. "
        "Use the available tools to look up orders, process refunds, "
        "search the knowledge base, and calculate loyalty discounts. "
        "Be concise and accurate."
    )

    logger.info(f"Connecting to Gateway at {GATEWAY_URL}")
    try:
        gateway_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))
        with gateway_client:
            try:
                gateway_tools = gateway_client.list_tools_sync()
                logger.info(f"Loaded {len(gateway_tools)} tools from Gateway")
            except Exception as e:
                logger.exception("Failed to load tools from Gateway")
                return (
                    "I'm sorry, I'm having trouble reaching our backend services "
                    "right now (order lookups and refunds may be unavailable). "
                    "Please try again in a moment."
                )

            all_tools = tools + gateway_tools

            agent = Agent(
                model=model,
                tools=all_tools,
                hooks=[memory_hook],
                system_prompt=system_prompt,
            )

            response = await agent.invoke_async(user_input)
            return response.message["content"][0]["text"]

    except Exception as e:
        logger.exception("Agent invocation failed")
        return f"I'm sorry, I encountered an error processing your request: {e}"


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()
