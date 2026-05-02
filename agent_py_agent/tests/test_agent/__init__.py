"""LLM: Test package for agent core behavior; pytest discovers tests by file pattern.

给人看的解释：
把原来大文件 test_agent.py 拆成多个小模块，方便维护。
pytest 会按文件名模式自动发现测试，不需要在 __init__.py 里显式导出。
"""
