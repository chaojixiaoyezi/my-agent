"""service objects composed by SubAgentManager.

这个包承接从 manager mixin 中抽出的稳定职责。外部仍使用 SubAgentManager，
service 只在内部收口实现细节。导入时直接走各实现模块，不经过本文件转发。
"""
