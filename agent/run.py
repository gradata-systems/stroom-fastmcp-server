"""Run the build agent from a terminal against the Stroom MCP server.

    uv run --extra agent python -m agent.run --sample sample.csv "Onboard Acme VPN logs"

Environment:
  AGENT_MCP_URL         MCP endpoint, e.g. https://stroom-mcp.example.com/mcp
  AGENT_TOKEN_URL       Keycloak token endpoint (client credentials), e.g.
                        https://keycloak.example.com/realms/security/protocol/openid-connect/token
  AGENT_CLIENT_ID / AGENT_CLIENT_SECRET   the agent's confidential client
  AGENT_BEARER          or a ready-made bearer token (development)
  AGENT_MODEL           e.g. 'openai:gpt-4.1' or 'anthropic:claude-sonnet-5'; an OpenAI-compatible
                        server (vLLM) with AGENT_MODEL_BASE_URL and OPENAI_API_KEY
Interrupts (confirmations, approvals, requests for help) are asked on the terminal.
"""
import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path

import httpx
from langchain.chat_models import init_chat_model
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent.graph import build_graph
from agent.mcp_tools import load_tools


async def token() -> str:
    if os.environ.get('AGENT_BEARER'):
        return os.environ['AGENT_BEARER']
    async with httpx.AsyncClient() as client:
        response = await client.post(os.environ['AGENT_TOKEN_URL'], data={
            'grant_type': 'client_credentials', 'client_id': os.environ['AGENT_CLIENT_ID'],
            'client_secret': os.environ['AGENT_CLIENT_SECRET']})
        response.raise_for_status()
        return response.json()['access_token']


def ask(payload: dict) -> dict:
    print(f"\n--- {payload.get('kind')}: {payload.get('summary')}")
    if payload.get('details'):
        print(json.dumps(payload['details'], indent=1, default=str))
    if payload.get('kind') == 'help':
        return {'note': input('Hint for the agent: ')}
    answer = input('Agree? [y/N, or type a correction] ').strip()
    return {'approved': answer.lower() in ('y', 'yes')} if answer.lower() in ('y', 'yes', 'n', 'no', '') \
        else {'approved': False, 'note': answer}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('request')
    parser.add_argument('--sample', type=Path)
    parser.add_argument('--docs', type=Path, help='vendor documentation or annotated sample notes')
    args = parser.parse_args()
    request = args.request
    if args.sample:
        request += f"\n\nSample:\n{args.sample.read_text(encoding='utf-8')}"
    if args.docs:
        request += f"\n\nSource documentation:\n{args.docs.read_text(encoding='utf-8')}"

    async with Client(os.environ['AGENT_MCP_URL'], auth=BearerAuth(await token())) as client:
        tools = await load_tools(client)
        model = init_chat_model(os.environ.get('AGENT_MODEL', 'openai:gpt-4.1'),
                                base_url=os.environ.get('AGENT_MODEL_BASE_URL'))
        graph = build_graph(model, tools, checkpointer=MemorySaver())
        config = {'configurable': {'thread_id': str(uuid.uuid4())}, 'recursion_limit': 200}
        command: object = {'mode': 'onboard', 'request': request}
        while True:
            result = await graph.ainvoke(command, config)
            pending = result.get('__interrupt__')
            if not pending:
                break
            command = Command(resume=ask(pending[0].value))
        print('\n'.join(result.get('notes') or []))


if __name__ == '__main__':
    asyncio.run(main())
