from src.agents.base import BaseAgent
from src.prompts.mysql_agent import MYSQL_AGENT_SYSTEM, MYSQL_AGENT_USER_TEMPLATE


class MySQLAgent(BaseAgent):
    name = "mysql"
    system_prompt = MYSQL_AGENT_SYSTEM
    user_template = MYSQL_AGENT_USER_TEMPLATE
    servers = ["mysql"]
