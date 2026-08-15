from __future__ import annotations

"""my-agent feishu connect:一条命令接入飞书。

两条路:
  --scan                     扫码自助建应用(飞书侧自动创建应用并回传 app_id/secret,免去手工建应用)
  --app-id X --app-secret Y  直接给已有凭据

拿到凭据后:写进配置(--config,长连接模式),可选同时写进 systemd 部署的密钥 env 文件
(--secret-file),可选写完直接重启服务(--restart)连上飞书。

这是「让 agent / 小白一条命令(甚至一句话)接飞书」的入口——对齐 通道运行时/长期助手 的扫码建应用,
但做成 agent 可自调的 CLI(agent 能 run_command 跑它),而不是只给人用的向导。
"""

import argparse
import os
import subprocess
from pathlib import Path

from agent_py_agent.agent.adapter.feishu_app_registration import (
    AppRegistrationResult,
    BeginResult,
    FeishuRegistrationError,
    register_feishu_app_by_scan,
)
from agent_py_agent.agent.settings.config_io import set_simple_yaml_value


def add_feishu_subcommands(sub: argparse._SubParsersAction) -> None:
    feishu = sub.add_parser("feishu", help="飞书接入:扫码自助建应用 / 设凭据并连接")
    feishu_sub = feishu.add_subparsers(dest="feishu_command")
    connect = feishu_sub.add_parser(
        "connect", help="接入飞书:--scan 扫码自动建应用,或 --app-id/--app-secret 直接给凭据"
    )
    connect.add_argument("--scan", action="store_true", help="扫码自助建应用(飞书自动建并回传密钥)")
    connect.add_argument("--app-id", help="直接提供 feishu app_id")
    connect.add_argument("--app-secret", help="直接提供 feishu app_secret")
    connect.add_argument(
        "--domain", choices=["feishu", "lark"], default="feishu", help="域名:feishu(国内)/lark(国际)"
    )
    connect.add_argument(
        "--secret-file",
        help="同时把凭据写进 systemd 部署用的密钥 env 文件(如 /etc/my-agent/feishu.env)",
    )
    connect.add_argument(
        "--restart", action="store_true", help="写完直接重启 systemd 服务(my-agent-gateway/feishu)连上飞书"
    )
    connect.set_defaults(func=cmd_feishu_connect)


def _mask(secret: str) -> str:
    return f"{secret[:3]}***" if secret and len(secret) > 3 else "***"


def _on_qr(begin: BeginResult, qr_ascii: str) -> None:
    print("\n请用【飞书 / Lark App】扫下面的码授权(飞书会自动建好应用):\n")
    if qr_ascii:
        print(qr_ascii)
    else:
        print("(未装 qrcode 库渲染不了二维码;请用手机打开下面链接,或把链接粘进任意二维码生成器)")
    print(f"链接: {begin.qr_url}")
    if begin.user_code:
        print(f"user_code: {begin.user_code}")
    print(f"\n等待扫码授权中(最多 {begin.expire_in}s)…\n")


def _write_secret_env(path: str, app_id: str, app_secret: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'FEISHU_APP_ID="{app_id}"\nFEISHU_APP_SECRET="{app_secret}"\n', encoding="utf-8")
    os.chmod(p, 0o600)


def _restart_services() -> bool:
    if not Path("/usr/bin/systemctl").exists() and not Path("/bin/systemctl").exists():
        print("⚠️ 没找到 systemctl,跳过自动重启;请手动重启服务。")
        return False
    ok = True
    for svc in ("my-agent-gateway.service", "my-agent-feishu.service"):
        try:
            subprocess.run(["systemctl", "restart", svc], check=True, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            print(f"⚠️ 重启 {svc} 失败:{exc}")
            ok = False
    return ok


def cmd_feishu_connect(args) -> int:
    # 1) 取得凭据:扫码 or 直接给
    if args.scan:
        try:
            result: AppRegistrationResult = register_feishu_app_by_scan(
                domain=args.domain, on_qr=_on_qr
            )
        except FeishuRegistrationError as exc:
            print(f"❌ 扫码建应用失败:{exc}")
            return 1
        except Exception as exc:  # 网络/解析等
            print(f"❌ 扫码建应用出错:{exc}")
            return 1
        app_id, app_secret, domain, open_id = (
            result.app_id, result.app_secret, result.domain, result.open_id,
        )
        print(f"✅ 应用已自动创建:app_id={app_id}  domain={domain}" + (f"  owner={open_id}" if open_id else ""))
    elif args.app_id and args.app_secret:
        app_id, app_secret, domain, open_id = args.app_id, args.app_secret, args.domain, None
    else:
        print("❌ 需要 --scan(扫码自助建应用)或 --app-id+--app-secret(直接给凭据)其一。")
        return 1

    if domain == "lark":
        print("⚠️ 你的租户是 Lark(国际版);当前 my-agent 飞书适配默认连 feishu.cn 网关,Lark 需另配域名(暂未支持)。")

    # 2) 写进配置(长连接模式)
    cfg = Path(args.config)
    if not cfg.exists():
        print(f"❌ 配置文件不存在:{cfg}")
        return 1
    set_simple_yaml_value(cfg, "feishu_app_id", app_id)
    set_simple_yaml_value(cfg, "feishu_app_secret", app_secret)
    set_simple_yaml_value(cfg, "feishu_connection_mode", "long_connection")
    print(f"✅ 凭据写入配置 {cfg}(app_id={app_id} secret={_mask(app_secret)} 长连接模式)")

    # 3) 可选:写部署密钥 env 文件
    if args.secret_file:
        _write_secret_env(args.secret_file, app_id, app_secret)
        print(f"✅ 凭据写入密钥文件 {args.secret_file}(600)")

    # 4) 可选:重启服务连上
    if args.restart:
        print("↻ 重启 systemd 服务…")
        if _restart_services():
            print("✅ 服务已重启,飞书长连接应在数秒内建立(看 journalctl -u my-agent-feishu)。")
    else:
        print("ℹ️ 下一步:重启网关+适配器使其生效(systemctl restart my-agent-gateway my-agent-feishu,或 --restart)。")
    return 0


__all__ = ["add_feishu_subcommands", "cmd_feishu_connect"]
