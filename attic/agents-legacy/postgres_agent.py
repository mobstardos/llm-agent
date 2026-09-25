from src.agents.base import BaseAgent
from src.prompts.postgres_agent import POSTGRES_AGENT_SYSTEM, POSTGRES_AGENT_USER_TEMPLATE


class PostgresAgent(BaseAgent):
    name = "postgres"
    system_prompt = POSTGRES_AGENT_SYSTEM
    user_template = POSTGRES_AGENT_USER_TEMPLATE
    servers = ["postgres"]
