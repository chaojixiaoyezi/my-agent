# repositories

LLM: future repository layer for domain-specific persistence interfaces.

给人看的解释：
当某个领域的数据访问变复杂时，把读写细节放这里，向上层提供稳定接口。
repository 只管查询和保存，不做状态机决策。
