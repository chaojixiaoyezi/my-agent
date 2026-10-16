# LLM: 入站图片只消费已认证下载器写入的owner文件；同次准备结果不保存第二权限或重试账，只有整组成功才可冻结到Gateway。
# 模块用途: 有界读取和验证入站图片，生成原input_media内容寻址引用，并提供可永久结算的客观输入错误。
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..conversation.input_media import InputMediaError, import_input_media_bytes


# LLM: 仅确定的图片格式/数量/字节错误使用此类型；owner、路径及暂态下载错误不得转换到这里。
# 类用途: 让原入站worker永久拒绝本次无效图片，而非无限重试或丢图继续模型任务。
class InboundMediaInputError(InputMediaError):
    error_code = 'INPUT_MEDIA_INVALID'

    # LLM: message由宿主固定说明提供，不接受供应商正文、文件路径或异常原文。
    # 函数用途: 保存可公开的协议拒绝说明，不携带图片内容或秘密。
    def __init__(self, message: str):
        super().__init__(message)


# LLM: 对象仅属于一次prepare调用，不能跨请求缓存；refs只有全部下载成功后才由manager写入原payload_ready。
# 类用途: 跟踪当前及引用图片的共同预算，并把真实解码后的图片导入原owner媒体目录。
@dataclass
class InboundImagePreparation:
    max_bytes: int
    max_files: int
    max_pixels: int
    refs: list[dict] = field(default_factory=list)
    total_bytes: int = 0

    # LLM: 预算在实际HTTP读取前给出，不能先无限下载后才检查；同次所有图片共用剩余字节。
    # 函数用途: 拒绝超量图片并返回下一资源的实际下载上限。
    def download_limit(self) -> int:
        if len(self.refs) >= self.max_files:
            raise InboundMediaInputError('本条消息图片数量超过上限，请减少图片后重新发送。')
        if self.total_bytes >= self.max_bytes:
            raise InboundMediaInputError('本条消息图片总大小超过上限，请压缩图片后重新发送。')
        return self.max_bytes - self.total_bytes

    # LLM: 根目录由原owner解析提供，叶子来自原下载器；no-follow失败原样抛出，不能被当成可忽略的坏图。
    # 函数用途: 读取有界图片、核真实格式和像素，再原子导入内容寻址目录；返回本次真实引用。
    def import_download(self, path: Path) -> dict:
        from ..conversation.input_media_images import inspect_input_image

        try:
            data = read_bytes_beneath(path.parent, (path.name,), max_bytes=self.download_limit(), require_dir_fd=True)
        except ValueError as exc:
            raise InboundMediaInputError('本条消息图片总大小超过上限，请压缩图片后重新发送。') from exc
        if data is None:
            raise OSError('INBOUND_MEDIA_DOWNLOAD_INCOMPLETE')
        try:
            media_type = inspect_input_image(data, max_pixels=self.max_pixels)
        except InputMediaError as exc:
            raise InboundMediaInputError('图片损坏、格式不支持或像素超过上限，请重新发送可读取的图片。') from exc
        ref = import_input_media_bytes(data, path.parent, media_type=media_type, name=path.name)
        self.refs.append(ref)
        self.total_bytes += len(data)
        return ref


# LLM: 唯一配置来自已绑定原宿主AgentConfig；轻量嵌入默认与正式配置一致，不从消息metadata读取开关或额度。
# 函数用途: 给一次飞书prepare创建可选图片预算，关闭时保留原附件路径行为。
def inbound_image_preparation(agent: object, channel: str) -> InboundImagePreparation | None:
    config = getattr(agent, 'config', None)
    if channel != 'feishu' or getattr(config, 'feishu_image_input_enabled', True) is not True:
        return None
    return InboundImagePreparation(
        max_bytes=int(getattr(config, 'input_media_max_bytes', 16 * 1024 * 1024)),
        max_files=int(getattr(config, 'input_media_max_files', 8)),
        max_pixels=int(getattr(config, 'input_image_max_pixels', 40_000_000)),
    )
