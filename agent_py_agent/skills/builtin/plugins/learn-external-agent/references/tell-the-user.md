# 装完以后怎么跟用户说（照 package_install 回执原文填）

用户不会记命令。每条命令都原样给出，并说清它做什么、怎么撤回。不要凭记忆说开关状态，只看回执里的 `switches`；装的是哪一版也照回执 `message` 说（宿主写的版本）。

## 回执 state = installed_enabled（开关开着，已经装好）

- 装了什么：照 `message` 说包名和版本；来源和许可证是你自己声明的，照实说是"我从哪里学的"。
- 以后怎么用：相关任务会推荐它，也可以点名让我用。查看全部能力包发 `/plugins#`。
- 不想要了：先发 `manage_commands.disable` 停用；彻底删掉发 `manage_commands.remove`。
- 想回到上一版：发 `manage_commands.revert`。它只会先告诉你退到哪一版、会不会运行程序，再给一行带版本的命令，发那一行才真退。
- 不想让我以后自己装：发 `switches.switch_commands.capability_pack_off`（插件是 `plugin_off`），改完马上生效。

## 回执 state = awaiting_user_confirmation（开关关着，没装）

- 说明：包已经做好，但自动装开关关着，所以没装。
- 现在装这一个：把 `user_confirm_command` 原样发给我（能力包是 `/plugins#<包名> 安装 <单号>`，插件是 `/plugins confirm <单号>`）。这一行隔多久发都有效，只执行一次；但只有我最后给的那一行有效，我后来又出了新单，旧的就作废。
- 以后都让我自己装：发 `enable_auto_install_command`；想关回去发对应的 `..._off` 命令。
- 回执里有 `installed_now`：告诉用户现在装着的是哪一版，确认后会换成这一版。
- 不想装：不用理它。
- 如果 `package.runs_programs` 为 true：提醒用户这个包会运行它自带的程序（插件的程序或能力包的检查程序），发确认就是同意运行。

## 回执 state = installed_needs_user_confirmation（装上了，停在启用确认）

- 说明为什么停：它要联网或读写别的目录，或者只能不受限运行——这些我不能替你同意。
- 把 `next_command` 原样给用户，让他核对预览后自己发。

## 失败（ok = false）

- 照 `message` 和 `error_code` 说，有 `request_ids` 就照实列出来；不要自己反复重试安装。
- `PACKAGE_INSTALL_ID_TAKEN`：包名被别处的同名包占着，换一个包名重新打包。
