from src.agents.base import BaseAgent
from src.prompts.deepseek_agent import DEEPSEEK_AGENT_SYSTEM, DEEPSEEK_AGENT_USER_TEMPLATE


class DeepSeekAgent(BaseAgent):
    name = "deepseek"
    system_prompt = DEEPSEEK_AGENT_SYSTEM
    user_template = DEEPSEEK_AGENT_USER_TEMPLATE
    servers = ["deepseek_web"]
