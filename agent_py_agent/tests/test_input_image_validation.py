from __future__ import annotations

import io
import struct
import zlib

import pytest
from PIL import Image

from agent_py_agent.agent.conversation.input_media import InputMediaError
from agent_py_agent.agent.conversation.input_media_images import inspect_input_image
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.normalize import normalize_agent_config


# LLM: 只在测试内生成小尺寸真实图片，不使用用户文件、网络或外部解释器。
# 函数用途: 构造能被真实解码器读取的格式样本，避免假png字节掩盖图片校验缺口。
def _image_bytes(kind: str = "PNG", *, size: tuple[int, int] = (4, 3)) -> bytes:
    output = io.BytesIO()
    with Image.new("RGB", size, color=(31, 67, 127)) as image:
        image.save(output, format=kind)
    return output.getvalue()


@pytest.mark.parametrize("kind,mime", [
    ("PNG", "image/png"), ("JPEG", "image/jpeg"),
    ("WEBP", "image/webp"), ("GIF", "image/gif"), ("BMP", "image/bmp"),
])
def test_actual_decoder_format_not_extension_or_closed_enum(kind, mime):
    assert inspect_input_image(_image_bytes(kind)) == mime


@pytest.mark.parametrize("data", [b"", b"not an image", b"\x89PNG\r\n\x1a\n", b"<svg></svg>"])
def test_invalid_bytes_are_permanent_input_errors(data):
    with pytest.raises(InputMediaError):
        inspect_input_image(data)


def test_truncated_image_rejected():
    with pytest.raises(InputMediaError):
        inspect_input_image(_image_bytes()[:-14])


def test_real_pixel_budget_checked_before_load(monkeypatch):
    data = _image_bytes(size=(10, 10))
    monkeypatch.setattr(Image.Image, "load", lambda self: pytest.fail("over-budget image decoded"))
    with pytest.raises(InputMediaError, match="累计像素"):
        inspect_input_image(data, max_pixels=99)


def test_corrupt_png_checksum_rejected():
    data = bytearray(_image_bytes())
    data[29] ^= 1
    with pytest.raises(InputMediaError):
        inspect_input_image(bytes(data))


def test_animated_image_uses_sum_of_frames():
    output = io.BytesIO()
    first, second = Image.new("RGB", (3, 2), "red"), Image.new("RGB", (3, 2), "blue")
    first.save(output, format="GIF", save_all=True, append_images=[second], duration=10)
    assert inspect_input_image(output.getvalue(), max_pixels=12) == "image/gif"
    with pytest.raises(InputMediaError, match="累计像素"):
        inspect_input_image(output.getvalue(), max_pixels=11)


def test_extreme_dimensions_rejected_without_allocating_pixels():
    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", 50_000, 50_000, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    data = signature + struct.pack(">I", len(header)) + chunk + struct.pack(">I", zlib.crc32(chunk))
    with pytest.raises(InputMediaError):
        inspect_input_image(data)


def test_frame_count_bounded_before_loading_all_frames(monkeypatch):
    from agent_py_agent.agent.conversation import input_media_images

    monkeypatch.setattr(input_media_images, "MAX_IMAGE_FRAMES", 1)
    output = io.BytesIO()
    images = [Image.new("RGB", (1, 1), color) for color in ("red", "blue")]
    images[0].save(output, format="GIF", save_all=True, append_images=images[1:])
    with pytest.raises(InputMediaError, match="帧数"):
        inspect_input_image(output.getvalue())


def test_external_interpreter_format_is_never_executed(monkeypatch):
    from PIL import EpsImagePlugin

    monkeypatch.setattr(EpsImagePlugin, "Ghostscript", lambda *args, **kwargs: pytest.fail("external interpreter"))
    data = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n%%EndComments\nshowpage\n"
    with pytest.raises(InputMediaError, match="安全解码"):
        inspect_input_image(data)


@pytest.mark.parametrize("raw,expected", [(True, True), (False, False), ("false", False), ("true", True)])
def test_image_input_switch_normalized(raw, expected):
    values, warnings = normalize_agent_config({"feishu_image_input_enabled": raw})
    assert values["feishu_image_input_enabled"] is expected
    assert not warnings


@pytest.mark.parametrize("raw", [0, -1, "invalid", True])
def test_invalid_pixel_limit_uses_same_config_default(raw):
    values, warnings = normalize_agent_config({"input_image_max_pixels": raw})
    assert values["input_image_max_pixels"] == AgentConfig().input_image_max_pixels
    assert warnings


def test_config_yaml_dataclass_and_normalization_agree():
    from pathlib import Path

    from agent_py_agent.agent.settings.config import load_simple_yaml
    config_path = Path(__file__).parents[1] / "config" / "agent_config.yaml"
    raw = load_simple_yaml(config_path)
    values, warnings = normalize_agent_config({
        key: raw[key] for key in ("feishu_image_input_enabled", "input_image_max_pixels")
    })
    assert not warnings
    assert values["feishu_image_input_enabled"] is AgentConfig().feishu_image_input_enabled is True
    assert values["input_image_max_pixels"] == AgentConfig().input_image_max_pixels == 40_000_000
