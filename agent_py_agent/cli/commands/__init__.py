"""LLM: command-registration modules live here to keep parser.py small.

给人看的解释：
这个包逐步承接 argparse 子命令注册。命令实现仍留在原来的业务模块里，
迁移目标是让 parser.py 只负责编排，而不是承载所有命令细节。
"""

